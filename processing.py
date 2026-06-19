"""
Общая обработка тикета: текст → RAG-инструкция → рассылка на ревью в Telegram.

Используется в двух местах:
  • webhook.py            — авто-приём (HelpDesk шлёт событие на /helpdesk/webhook);
  • tgbot команда /fetch  — ручная подтяжка тикетов оператором (fetch_and_review).

К HelpDesk идут только GET-запросы. Ответ клиенту здесь не пишется — это делает
оператор после ревью в Telegram.
"""
from __future__ import annotations

import datetime
import logging
import time

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
    daily_counter: bool = False,
) -> bool:
    """Обработать один тикет и отправить на ревью.

    text пустой → дотягиваем из HelpDesk (GET). skip_existing=True → пропускаем
    уже отправленные тикеты (дедуп для поллера). daily_counter=True → в сообщение
    добавляется счётчик «N-й тикет за сегодня» (для авто-приёма из поллера).
    Возвращает True, если сообщение в Telegram отправлено.
    """
    task_id = str(task_id)
    if skip_existing and store.exists(task_id):
        log.debug("ticket %s уже обработан, пропуск", task_id)
        return False

    text = (text or "").strip()
    if not text:
        t_fetch = time.perf_counter()
        fetched = get_ticket_text(task_id)
        text = (fetched.get("text") or "").strip()
        subject = subject or fetched.get("subject")
        log.info(
            "ticket %s: текст дотянут из HelpDesk за %.2fc",
            task_id,
            time.perf_counter() - t_fetch,
        )
    if not text:
        log.warning("ticket %s: пустой текст, пропуск", task_id)
        return False

    # Порядковый номер за сегодня = уже сохранённые сегодня + этот (ещё не записан).
    daily_index = store.count_today() + 1 if daily_counter else None

    t_start = time.perf_counter()
    answer = get_pipeline().answer(text)
    t_answer = time.perf_counter() - t_start
    t_send_start = time.perf_counter()
    send_for_review(
        ticket_id=task_id,
        instruction=answer.instruction,
        ticket_text=text,
        subject=subject,
        sources=answer.sources,
        model=answer.model,
        chat_id=chat_id,
        solution_found=answer.solution_found,
        unknown_terms=answer.unknown_terms,
        daily_index=daily_index,
    )
    t_send = time.perf_counter() - t_send_start
    log.info(
        "ticket %s обработан: RAG %.2fc, отправка %.2fc, всего %.2fc",
        task_id,
        t_answer,
        t_send,
        time.perf_counter() - t_start,
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


_DT_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_dt(s: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime((s or "").strip(), _DT_FMT)
    except (TypeError, ValueError):
        return None


def _ticket_date(ticket: dict) -> str:
    """date_created тикета в формате HelpDesk ('YYYY-MM-DD HH:MM:SS') или ''."""
    return str(ticket.get("date_created") or "").strip()


def process_new_tickets(chat_id: str | int | None = None) -> dict:
    """Поллер: обработать ТОЛЬКО новые тикеты (date_created новее водяного знака).

    «Новизну» определяем по дате создания: запрашиваем у HelpDesk тикеты с
    from_date_created = знак (фильтр включающий, проверено на API). Сервер сам
    отдаёт только свежие тикеты, поэтому усечение по лимиту не теряет новые —
    в отличие от выборки всех открытых. Дедуп уже обработанных — через
    store.exists() (он же страхует от повторной отправки на границе секунды,
    т.к. фильтр включающий и тикеты «ровно на знаке» приходят снова).

    Водяной знак (date_created последнего тикета) хранится в store.meta и
    переживает перезапуски — тикеты из простоя поллера подхватятся на следующем
    проходе. Первый запуск только ставит знак «сейчас» и НЕ шлёт накопившийся
    бэклог. Сводка: {found, sent, skipped, empty, watermark, initialized}.
    """
    raw = store.get_meta(store.META_POLLER_LAST_DATE)
    statuses = config.fetch_statuses
    limit = config.fetch_limit
    sent: list[str] = []
    skipped: list[str] = []
    empty: list[str] = []

    with make_client() as client:
        # Первый запуск: знак = max(now, самый свежий из бэклога) + 1с, чтобы
        # включающий фильтр на следующем проходе НЕ зацепил текущий бэклог
        # (в т.ч. тикеты в ту же секунду). Бэклог не рассылаем.
        if raw is None:
            backlog = fetch_tickets(client, statuses, limit=limit)
            newest = max(
                (d for t in backlog if (d := _parse_dt(_ticket_date(t)))), default=None
            )
            base = max(datetime.datetime.now(), newest) if newest else datetime.datetime.now()
            watermark = (base + datetime.timedelta(seconds=1)).strftime(_DT_FMT)
            store.set_meta(store.META_POLLER_LAST_DATE, watermark)
            log.info("поллер: инициализация водяного знака = %s (бэклог пропущен)", watermark)
            return {
                "found": len(backlog),
                "sent": sent,
                "skipped": skipped,
                "empty": empty,
                "watermark": watermark,
                "initialized": True,
            }

        tickets = fetch_tickets(client, statuses, from_date=raw, limit=limit)
        # По возрастанию даты создания (затем id) — чтобы счётчик за сегодня шёл по порядку.
        tickets = sorted(
            tickets,
            key=lambda t: (_ticket_date(t), _parse_dt(_ticket_date(t)) is None, str(t.get("id"))),
        )
        watermark = raw
        for t in tickets:
            tid = str(t.get("id"))
            date = _ticket_date(t)
            if date > watermark:
                watermark = date  # строки YYYY-MM-DD HH:MM:SS сравниваются хронологически
            if store.exists(tid):
                skipped.append(tid)  # уже обработан (в т.ч. повторно пришедший «на знаке»)
                continue
            text = ticket_text(client, t)
            if process_ticket(
                tid,
                text,
                subject=ticket_subject(t),
                skip_existing=True,
                chat_id=chat_id,
                daily_counter=True,
            ):
                sent.append(tid)
            else:
                empty.append(tid)

    if watermark > raw:
        store.set_meta(store.META_POLLER_LAST_DATE, watermark)
    return {
        "found": len(tickets),
        "sent": sent,
        "skipped": skipped,
        "empty": empty,
        "watermark": watermark,
        "initialized": False,
    }
