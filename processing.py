"""
Общая обработка тикета: текст → RAG-инструкция → рассылка на ревью в Telegram.

Используется в двух местах:
  • webhook.py            — авто-приём (HelpDesk шлёт событие на /helpdesk/webhook);
  • tgbot команда /fetch  — ручная подтяжка тикетов оператором (fetch_and_review).

К HelpDesk идут только GET-запросы. Ответ клиенту здесь не пишется — это делает
оператор после ревью в Telegram.
"""
from __future__ import annotations

import logging

from config import config
from rag.llm import make_llm
from rag.pipeline import RagPipeline
from tgbot import store
from tgbot.notify import send_for_review
from tools.helpdesk_tools import (
    fetch_tickets,
    get_ticket_text,
    make_client,
    ticket_subject,
    ticket_text,
)

log = logging.getLogger(__name__)

# RAG-пайплайн тяжёлый на инициализацию (эмбеддинги/Qdrant) — создаём один раз.
_pipeline: RagPipeline | None = None
# Провайдер LLM, выбранный на лету (None → берём config.llm_provider).
_provider_override: str | None = None


def get_pipeline() -> RagPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = RagPipeline()
        if _provider_override:
            _pipeline.llm = make_llm(_provider_override)
    return _pipeline


def current_provider() -> str:
    """Активный LLM-провайдер (deepseek | ollama)."""
    return _provider_override or config.llm_provider


def set_provider(provider: str) -> str:
    """Переключить LLM-провайдер на лету. Возвращает нормализованное имя.

    Валидирует через make_llm (бросает ValueError на неизвестном провайдере).
    Если пайплайн уже создан — подменяет его llm-клиент.
    """
    provider = (provider or "").strip().lower()
    llm = make_llm(provider)  # ValueError при неизвестном провайдере
    global _provider_override
    _provider_override = provider
    if _pipeline is not None:
        _pipeline.llm = llm
    log.info("LLM-провайдер переключён на %s", provider)
    return provider


def process_ticket(
    task_id: str,
    text: str = "",
    subject: str | None = None,
    skip_existing: bool = False,
    chat_id: str | int | None = None,
) -> bool:
    """Обработать один тикет и отправить на ревью.

    text пустой → дотягиваем из HelpDesk (GET). skip_existing=True → пропускаем
    уже отправленные тикеты (дедуп для поллера).
    Возвращает True, если сообщение в Telegram отправлено.
    """
    task_id = str(task_id)
    if skip_existing and store.exists(task_id):
        log.debug("ticket %s уже обработан, пропуск", task_id)
        return False

    text = (text or "").strip()
    if not text:
        fetched = get_ticket_text(task_id)
        text = (fetched.get("text") or "").strip()
        subject = subject or fetched.get("subject")
    if not text:
        log.warning("ticket %s: пустой текст, пропуск", task_id)
        return False

    answer = get_pipeline().answer(text)
    send_for_review(
        ticket_id=task_id,
        instruction=answer.instruction,
        ticket_text=text,
        subject=subject,
        sources=answer.sources,
        model=answer.model,
        chat_id=chat_id,
    )
    return True


def list_open_tickets(
    statuses: str | None = None, limit: int | None = None
) -> list[dict]:
    """Список тикетов из HelpDesk (только GET) для выбора кнопками: [{id, subject}]."""
    statuses = statuses or config.fetch_statuses
    limit = limit or config.fetch_limit
    with make_client() as client:
        tickets = fetch_tickets(client, statuses, limit=limit)
    return [
        {"id": str(t["id"]), "subject": ticket_subject(t)}
        for t in tickets
        if t.get("id") is not None
    ]


def fetch_and_review(
    statuses: str | None = None,
    ids: list[str] | None = None,
    limit: int | None = None,
    chat_id: str | int | None = None,
) -> dict:
    """Ручная подтяжка тикетов из HelpDesk (только GET) → ревью в Telegram.

    ids заданы → тянем конкретные тикеты; иначе выборку по статусу.
    Уже отправленные на ревью пропускаем (дедуп). Возвращает сводку:
    {found, sent: [...], skipped: [...], empty: [...]}.
    """
    sent: list[str] = []
    skipped: list[str] = []
    empty: list[str] = []

    if ids:
        for tid in ids:
            tid = str(tid)
            if store.exists(tid):
                skipped.append(tid)
            elif process_ticket(tid, skip_existing=True, chat_id=chat_id):
                sent.append(tid)
            else:
                empty.append(tid)
        return {"found": len(ids), "sent": sent, "skipped": skipped, "empty": empty}

    statuses = statuses or config.fetch_statuses
    limit = limit or config.fetch_limit
    with make_client() as client:
        tickets = fetch_tickets(client, statuses, limit=limit)
        for t in tickets:
            tid = t.get("id")
            if tid is None:
                continue
            tid = str(tid)
            if store.exists(tid):
                skipped.append(tid)
                continue
            text = ticket_text(client, t)
            if process_ticket(
                tid, text, subject=ticket_subject(t), skip_existing=True, chat_id=chat_id
            ):
                sent.append(tid)
            else:
                empty.append(tid)
    return {"found": len(tickets), "sent": sent, "skipped": skipped, "empty": empty}
