"""
Личные команды бота: /stats, /ticket <id>, /pending, /help.

Чистые функции (parse + format) вынесены сюда, чтобы покрывать тестами без сети.
Диспетчер и отправка — в tgbot.bot.
"""
from __future__ import annotations

import html
from datetime import datetime

from .notify import ticket_url
from .store import (
    STATUS_APPROVE,
    STATUS_DECLINE,
    STATUS_EDITED,
    STATUS_PENDING,
)

STATUS_LABELS = {
    STATUS_PENDING: "⏳ Ожидает",
    STATUS_APPROVE: "✅ Approve",
    STATUS_DECLINE: "❌ Decline",
    STATUS_EDITED: "✍️ Свой ответ",
}

# Действия инлайн-кнопок-списков:
#   pick:<id> — выбрать тикет из /fetch → отправить на ревью в группу
#   open:<id> — открыть карточку тикета из /pending (с кнопками решения)
ACTION_PICK = "pick"
ACTION_OPEN = "open"

_TRUNC = 1500


def _short(text: str, n: int = 45) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def build_ticket_picker(items: list[dict], action: str, limit: int = 30) -> dict:
    """Клавиатура-список тикетов: по кнопке на тикет (callback '<action>:<id>').

    items — список словарей с ключом id|ticket_id и опц. subject.
    """
    rows = []
    for it in items[:limit]:
        tid = str(it.get("id") or it.get("ticket_id"))
        subj = _short(it.get("subject") or "")
        label = f"#{tid}" + (f" — {subj}" if subj else "")
        rows.append([{"text": label[:60], "callback_data": f"{action}:{tid}"}])
    return {"inline_keyboard": rows}


def parse_command(text: str | None) -> tuple[str, str] | None:
    """'/ticket 123' -> ('ticket', '123'); '/stats@Bot' -> ('stats', '').

    Не команда (не начинается с '/') -> None.
    """
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    cmd = head[1:].split("@", 1)[0].lower()  # убрать ведущий / и @botname
    return cmd, rest.strip()


def _ts(v) -> str:
    if not v:
        return "—"
    try:
        return datetime.fromtimestamp(float(v)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _truncate(text: str) -> str:
    text = text or ""
    return text if len(text) <= _TRUNC else text[:_TRUNC].rstrip() + "…"


def format_help() -> str:
    return (
        "<b>Команды бота</b>\n"
        "/fetch — список открытых тикетов кнопками (тап → на ревью)\n"
        "/fetch &lt;id&gt; [id…] — подтянуть конкретные тикеты\n"
        "/fetch limit &lt;N&gt; — подтянуть N открытых тикетов\n"
        "/pending — ожидающие решения (тап по кнопке открывает карточку)\n"
        "/ticket &lt;id&gt; — карточка тикета (с кнопками, если ещё не решён)\n"
        "/llm [deepseek|ollama] — показать или сменить LLM-провайдер\n"
        "/stats — статистика по ревью тикетов\n"
        "/chatid — показать id текущего чата\n"
        "/help — эта справка"
    )


def format_llm(current: str) -> str:
    return (
        f"<b>🧠 LLM-провайдер:</b> <code>{_esc(current)}</code>\n"
        "Доступно: deepseek, ollama\n"
        "Сменить: <code>/llm deepseek</code> или <code>/llm ollama</code>"
    )


def format_fetch_summary(s: dict) -> str:
    sent, skipped, empty = s.get("sent", []), s.get("skipped", []), s.get("empty", [])
    lines = [f"<b>🔎 Подтяжка тикетов</b> (найдено: {s.get('found', 0)})"]
    if sent:
        lines.append(f"✅ Отправлено на ревью ({len(sent)}): " + ", ".join(f"#{i}" for i in sent))
    if skipped:
        lines.append(f"⏭ Уже на ревью ({len(skipped)}): " + ", ".join(f"#{i}" for i in skipped))
    if empty:
        lines.append(f"⚠️ Пустой текст ({len(empty)}): " + ", ".join(f"#{i}" for i in empty))
    if not (sent or skipped or empty):
        lines.append("Новых тикетов нет.")
    return "\n".join(lines)


def format_stats(s: dict) -> str:
    bs = s.get("by_status", {})
    total = s.get("total", 0)
    pending = bs.get(STATUS_PENDING, 0)
    reviewed = total - pending
    lines = [
        "<b>📊 Статистика ревью</b>",
        f"Всего тикетов: <b>{total}</b>",
        f"{STATUS_LABELS[STATUS_PENDING]}: {pending}",
        f"{STATUS_LABELS[STATUS_APPROVE]}: {bs.get(STATUS_APPROVE, 0)}",
        f"{STATUS_LABELS[STATUS_DECLINE]}: {bs.get(STATUS_DECLINE, 0)}",
        f"{STATUS_LABELS[STATUS_EDITED]}: {bs.get(STATUS_EDITED, 0)}",
    ]
    if total:
        lines.append(f"Обработано: {reviewed}/{total} ({reviewed * 100 // total}%)")
    return "\n".join(lines)


def format_pending(rows: list[dict], limit: int = 20) -> str:
    if not rows:
        return "Нет тикетов, ожидающих решения. 🎉"
    lines = [f"<b>⏳ Ожидают решения ({len(rows)}):</b>"]
    for r in rows[:limit]:
        subj = _esc(r.get("subject") or "—")
        lines.append(f"• #{_esc(r['ticket_id'])} — {subj}")
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}")
    return "\n".join(lines)


def format_ticket(review: dict | None) -> str:
    if not review:
        return "Тикет не найден в базе ревью."
    tid = review["ticket_id"]
    label = STATUS_LABELS.get(review["status"], review["status"])
    lines = [
        f"<b>Тикет #{_esc(tid)}</b> — {label}",
        f'<a href="{_esc(ticket_url(tid))}">Открыть в HelpDesk</a>',
    ]
    if review.get("subject"):
        lines.append(f"Тема: {_esc(review['subject'])}")
    lines.append(f"Создан: {_ts(review.get('created_at'))}")
    if review.get("decided_at"):
        who = _esc(review.get("operator_name") or "—")
        lines.append(f"Решение: {_ts(review['decided_at'])} · {who}")

    lines += ["", "<b>Ответ ИИ:</b>", _esc(_truncate(review.get("ai_instruction") or "—"))]
    if review.get("operator_answer"):
        lines += ["", "<b>Ответ оператора:</b>", _esc(_truncate(review["operator_answer"]))]
    return "\n".join(lines)
