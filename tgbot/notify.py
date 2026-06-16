"""
Рассылка тикета на ревью в групповой Telegram-чат.

send_for_review(): формирует сообщение (ссылка на тикет + RAG-инструкция + источники),
инлайн-клавиатуру [Approve | Decline] / [Your answer], шлёт в TELEGRAM_CHAT_ID и
регистрирует тикет в store как pending.
"""
from __future__ import annotations

import html
import logging

from config import config

from . import store
from .client import BotClient

log = logging.getLogger(__name__)

# Действия инлайн-кнопок (callback_data = "<action>:<ticket_id>")
ACTION_APPROVE = "approve"
ACTION_DECLINE = "decline"
ACTION_ANSWER = "answer"

# Жёсткий предел текста сообщения Telegram (sendMessage) — 4096 символов,
# считается вместе с HTML-разметкой. Режем ровно по нему.
_MAX_TEXT = 4096


def build_keyboard(ticket_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"{ACTION_APPROVE}:{ticket_id}"},
                {"text": "❌ Decline", "callback_data": f"{ACTION_DECLINE}:{ticket_id}"},
            ],
            [
                {"text": "✍️ Your answer", "callback_data": f"{ACTION_ANSWER}:{ticket_id}"},
            ],
        ]
    }


def ticket_url(ticket_id: str) -> str:
    return config.helpdesk_eddy.ticket_url_template.format(id=ticket_id)


def parse_callback(data: str) -> tuple[str, str]:
    """'approve:123' -> ('approve', '123'). Пустой/битый -> ('', '')."""
    action, _, ticket_id = (data or "").partition(":")
    return action, ticket_id


def _format_sources(sources: list[dict] | None) -> str:
    if not sources:
        return ""
    lines = ["", "<i>Источники:</i>"]
    for s in sources[:6]:
        title = html.escape(str(s.get("title") or "—"))
        src = html.escape(str(s.get("source") or ""))
        lines.append(f"• {title} <code>[{src}]</code>")
    return "\n".join(lines)


def _format_unknown_terms(unknown_terms: list[str] | None) -> str:
    """Блок терминов из обращения, которых нет в базе знаний (модель их не поняла)."""
    if not unknown_terms:
        return ""
    lines = ["", "<i>⚠️ Терминов нет в базе знаний:</i>"]
    for t in unknown_terms[:15]:
        term = html.escape(str(t)).strip()
        if term:
            lines.append(f"• {term}")
    return "\n".join(lines)


# Плашка, когда прямой инструкции в базе знаний нет: ответ предполагаемый,
# инструкция всё равно приводится ниже (модель достроила её по общим принципам).
_NO_SOLUTION_BANNER = (
    "⚠️ <b>Прямой инструкции в базе знаний нет — ответ предположительный, "
    "проверьте перед отправкой.</b>"
)


def build_message(
    ticket_id: str,
    instruction: str,
    subject: str | None = None,
    sources: list[dict] | None = None,
    solution_found: bool = True,
    unknown_terms: list[str] | None = None,
    daily_index: int | None = None,
) -> str:
    head = f'<b>Тикет #{html.escape(str(ticket_id))}</b>'
    if subject:
        head += f": {html.escape(subject)}"
    link = f'<a href="{html.escape(ticket_url(ticket_id))}">Открыть тикет в HelpDesk</a>'

    body = html.escape(instruction or "—")
    sources_block = _format_sources(sources)
    terms_block = _format_unknown_terms(unknown_terms)

    # Инструкция приводится всегда (даже предполагаемая), поэтому заголовок единый.
    answer_label = "<b>Предлагаемый ответ:</b>"
    parts = [head, link]
    if daily_index is not None:
        # Счётчик за сегодня: этот тикет — N-й, и всего за сегодня пришло N.
        parts.append(f"📊 {daily_index}-й тикет за сегодня (всего сегодня: {daily_index})")
    parts.append("")
    if not solution_found:
        parts.append(_NO_SOLUTION_BANNER)
    parts += [answer_label, body]
    body_idx = len(parts) - 1
    if terms_block:
        parts.append(terms_block)
    if sources_block:
        parts.append(sources_block)

    # Точный бюджет на ответ: лимит минус всё остальное (заголовок, ссылка,
    # плашка, термины, источники, переводы строк) и минус символ «…».
    overhead = len("\n".join(parts)) - len(body) + 1
    budget = max(_MAX_TEXT - overhead, 0)
    if len(body) > budget:
        parts[body_idx] = body[:budget].rstrip() + "…"
    return "\n".join(parts)


def send_for_review(
    ticket_id: str,
    instruction: str,
    ticket_text: str,
    subject: str | None = None,
    sources: list[dict] | None = None,
    model: str | None = None,
    client: BotClient | None = None,
    chat_id: str | int | None = None,
    solution_found: bool = True,
    unknown_terms: list[str] | None = None,
    daily_index: int | None = None,
) -> int:
    """Отправить тикет на ревью и сохранить pending. Возвращает message_id.

    chat_id не задан → шлём в TELEGRAM_CHAT_ID (авто-приём из webhook).
    Для ручных действий бот передаёт чат, откуда пришёл запрос.
    """
    bot = client or BotClient()
    chat_id = chat_id or config.telegram.chat_id
    if not chat_id:
        raise ValueError("Не задан TELEGRAM_CHAT_ID")

    text = build_message(
        ticket_id,
        instruction,
        subject=subject,
        sources=sources,
        solution_found=solution_found,
        unknown_terms=unknown_terms,
        daily_index=daily_index,
    )
    result = bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=build_keyboard(ticket_id),
    )
    message_id = int(result["message_id"])

    store.create_pending(
        ticket_id=ticket_id,
        chat_id=chat_id,
        message_id=message_id,
        ticket_text=ticket_text,
        ai_instruction=instruction,
        ai_sources=sources,
        ai_model=model,
        subject=subject,
    )
    # регистрируем сообщение с кнопками — чтобы погасить его при решении
    store.add_ticket_message(ticket_id, chat_id, message_id)
    log.info("ticket #%s отправлен на ревью (message_id=%s)", ticket_id, message_id)
    return message_id
