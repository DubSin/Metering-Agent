"""
Telegram-бот ревью тикетов (long-polling).

Запуск:
    python -m tgbot.bot

Обрабатывает:
  • callback_query: Approve / Decline / Your answer, выбор тикета (pick/open);
  • message: команды (/fetch, /pending, /ticket, /stats, /llm, /help) и
    текстовый ответ оператора (reply на ForceReply-приглашение).

Команды и ответы бот отправляет в тот же чат, откуда пришёл запрос (в группе —
в группу, в личке — в личку). Рассылка тикетов на ревью идёт в TELEGRAM_CHAT_ID.
Решения пишутся в SQLite (см. tgbot.store) — это датасет для дообучения.
"""
from __future__ import annotations

import logging
import time

from config import config
from processing import (
    current_provider,
    fetch_and_review,
    list_open_tickets,
    process_ticket,
    set_provider,
)

from . import store
from .client import BotClient, TelegramError
from .commands import (
    ACTION_OPEN,
    ACTION_PICK,
    build_ticket_picker,
    format_fetch_summary,
    format_help,
    format_llm,
    format_pending,
    format_stats,
    format_ticket,
    parse_command,
)
from .notify import (
    ACTION_ANSWER,
    ACTION_APPROVE,
    ACTION_DECLINE,
    build_keyboard,
    parse_callback,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("tgbot")


def is_allowed(user: dict) -> bool:
    """Разрешён ли пользователю доступ к боту (белый список из config).

    Пустой список = открытый режим (пускаем всех). Совпадение по numeric id
    или по @username (без учёта регистра).
    """
    allowed = config.telegram.allowed_users
    if not allowed:
        return True
    uid = str(user.get("id", ""))
    uname = (user.get("username") or "").lower()
    return uid in allowed or (bool(uname) and uname in allowed)


def operator_name(user: dict) -> str:
    """Человекочитаемое имя оператора из объекта Telegram user."""
    if user.get("username"):
        return f"@{user['username']}"
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return name or str(user.get("id", "—"))


def _decided_keyboard(label: str) -> dict:
    """Клавиатура из одной неактивной кнопки-метки (callback → noop)."""
    return {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}


class ReviewBot:
    def __init__(self, client: BotClient | None = None) -> None:
        self.bot = client or BotClient()

    # ------------------------------------------------------------------ #
    # callback_query
    # ------------------------------------------------------------------ #
    def handle_callback(self, cq: dict) -> None:
        action, ticket_id = parse_callback(cq.get("data") or "")
        user = cq.get("from") or {}
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        who = operator_name(user)

        if not is_allowed(user):
            self._ack(cq["id"], "Нет доступа")
            return

        if action == "noop" or not ticket_id:
            self._ack(cq["id"])
            return

        # выбор тикета из списка /fetch → отправить на ревью в группу
        if action == ACTION_PICK:
            self._ack(cq["id"], "Отправляю на ревью…")
            try:
                # ревью уходит в тот же чат, где нажали кнопку
                sent = process_ticket(ticket_id, skip_existing=True, chat_id=chat_id)
            except Exception as e:
                log.exception("pick #%s failed", ticket_id)
                self.bot.send_message(chat_id, f"Ошибка по #{ticket_id}: {e}")
                return
            txt = (
                f"Тикет #{ticket_id} отправлен на ревью."
                if sent
                else f"Тикет #{ticket_id} уже на ревью."
            )
            self.bot.send_message(chat_id, txt, reply_to_message_id=message_id)
            return

        # открыть карточку тикета из /pending (с кнопками решения, если ещё открыт)
        if action == ACTION_OPEN:
            self._ack(cq["id"])
            review = store.get(ticket_id)
            if not review:
                self.bot.send_message(
                    chat_id, "Тикет не найден в базе.", reply_to_message_id=message_id
                )
                return
            self._send_ticket_card(chat_id, review)
            return

        if action in (ACTION_APPROVE, ACTION_DECLINE):
            ok = store.record_decision(
                ticket_id, action, operator_id=user.get("id"), operator_name=who
            )
            if not ok:
                self._ack(cq["id"], "Тикет не найден в базе")
                return
            label = (
                f"✅ Approved · {who}"
                if action == ACTION_APPROVE
                else f"❌ Declined · {who}"
            )
            self._close_ticket(ticket_id, label)
            self._ack(cq["id"], "Сохранено")
            log.info("ticket #%s: %s by %s", ticket_id, action, who)

        elif action == ACTION_ANSWER:
            self._ack(cq["id"], "Ответьте на сообщение бота текстом")
            prompt = self.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✍️ {who}, ответьте <b>на это сообщение</b> текстом — "
                    f"он будет сохранён как ответ по тикету #{ticket_id}."
                ),
                reply_markup={"force_reply": True, "selective": False},
                reply_to_message_id=message_id,
            )
            store.link_answer_prompt(int(prompt["message_id"]), ticket_id)
            log.info("ticket #%s: запрошен ручной ответ от %s", ticket_id, who)
        else:
            self._ack(cq["id"])

    # ------------------------------------------------------------------ #
    # message
    # ------------------------------------------------------------------ #
    def handle_message(self, msg: dict) -> None:
        if not is_allowed(msg.get("from") or {}):
            return  # пользователь не в белом списке — молча игнорируем

        text = msg.get("text")

        parsed = parse_command(text)
        if parsed:
            self.handle_command(msg, *parsed)
            return

        # текстовый ответ оператора — только реплаем на сообщение-приглашение
        reply_to = msg.get("reply_to_message")
        if not reply_to or not text:
            return
        ticket_id = store.resolve_answer_prompt(int(reply_to["message_id"]))
        if not ticket_id:
            return  # обычное сообщение — не наш случай

        user = msg.get("from") or {}
        who = operator_name(user)
        store.set_operator_answer(
            ticket_id, text, operator_id=user.get("id"), operator_name=who
        )
        self._close_ticket(ticket_id, f"✍️ Answered · {who}")
        self.bot.send_message(
            chat_id=msg["chat"]["id"],
            text=f"Ответ по тикету #{ticket_id} сохранён. Спасибо, {who}.",
            reply_to_message_id=msg["message_id"],
        )
        log.info("ticket #%s: сохранён ручной ответ от %s", ticket_id, who)

    # ------------------------------------------------------------------ #
    # команды
    # ------------------------------------------------------------------ #
    def handle_command(self, msg: dict, cmd: str, args: str) -> None:
        chat_id = msg["chat"]["id"]
        reply_to = msg.get("message_id")

        if cmd in ("start", "help"):
            self._reply(chat_id, format_help(), reply_to)
        elif cmd == "chatid":
            chat = msg.get("chat") or {}
            self._reply(
                chat_id,
                f"chat_id: <code>{chat.get('id')}</code>\n"
                f"тип: {chat.get('type')}"
                + (f"\nназвание: {chat.get('title')}" if chat.get("title") else ""),
                reply_to,
            )
        elif cmd == "stats":
            self._reply(chat_id, format_stats(store.stats()), reply_to)
        elif cmd == "llm":
            self._reply(chat_id, self._do_llm(args), reply_to)
        elif cmd == "pending":
            rows = store.iter_reviews([store.STATUS_PENDING])
            markup = build_ticket_picker(rows, ACTION_OPEN) if rows else None
            self._reply(chat_id, format_pending(rows), reply_to, markup=markup)
        elif cmd == "ticket":
            tid = args.strip()
            if not tid:
                self._reply(chat_id, "Укажите номер: /ticket 110773", reply_to)
                return
            review = store.get(tid)
            if not review:
                self._reply(chat_id, "Тикет не найден в базе ревью.", reply_to)
                return
            self._send_ticket_card(chat_id, review, reply_to=reply_to)
        elif cmd == "fetch":
            self._handle_fetch(chat_id, args, reply_to)
        else:
            return  # неизвестная команда — молчим (в группе бывают чужие /cmd)
        log.info("команда /%s от chat=%s", cmd, chat_id)

    def _handle_fetch(self, chat_id, args: str, reply_to) -> None:
        # без аргументов — список открытых тикетов кнопками
        if not args.strip():
            try:
                tickets = list_open_tickets()
            except Exception as e:
                log.exception("/fetch list failed")
                self._reply(chat_id, f"Не удалось получить список тикетов: {e}", reply_to)
                return
            if not tickets:
                self._reply(chat_id, "Открытых тикетов нет.", reply_to)
                return
            self._reply(
                chat_id,
                f"<b>🔎 Открытые тикеты ({len(tickets)}):</b>\nТап по тикету — отправить на ревью.",
                reply_to,
                markup=build_ticket_picker(tickets, ACTION_PICK),
            )
            return
        # с аргументами — id / limit N / статус
        self._reply(chat_id, self._do_fetch(args, chat_id), reply_to)

    def _do_fetch(self, args: str, chat_id) -> str:
        """Подтянуть тикеты. args: 'limit N' | список id | имя статуса."""
        tokens = args.split()
        try:
            if tokens[0].lower() in ("limit", "lim") and len(tokens) > 1 and tokens[1].isdigit():
                summary = fetch_and_review(limit=int(tokens[1]), chat_id=chat_id)
            elif all(t.isdigit() for t in tokens):
                summary = fetch_and_review(ids=tokens, chat_id=chat_id)
            else:
                summary = fetch_and_review(statuses=tokens[0], chat_id=chat_id)
        except Exception as e:
            log.exception("/fetch failed")
            return f"Не удалось подтянуть тикеты: {e}"
        return format_fetch_summary(summary)

    def _do_llm(self, args: str) -> str:
        """Показать или сменить LLM-провайдер. args: пусто | deepseek | ollama."""
        arg = args.strip().lower()
        if not arg:
            return format_llm(current_provider())
        try:
            set_provider(arg)
        except ValueError as e:
            return f"⚠️ {e}"
        return f"✅ LLM переключён на: {arg}"

    # ------------------------------------------------------------------ #
    # вспомогательное
    # ------------------------------------------------------------------ #
    def _send_ticket_card(self, chat_id, review: dict, reply_to=None) -> None:
        """Карточка тикета; если ещё не решён — с кнопками Approve/Decline/Your answer."""
        pending = review.get("status") == store.STATUS_PENDING
        markup = build_keyboard(review["ticket_id"]) if pending else None
        res = self.bot.send_message(
            chat_id=chat_id,
            text=format_ticket(review),
            reply_markup=markup,
            reply_to_message_id=reply_to,
        )
        if markup and res:
            store.add_ticket_message(review["ticket_id"], chat_id, int(res["message_id"]))

    def _close_ticket(self, ticket_id: str, label: str) -> None:
        """Погасить кнопки во ВСЕХ сообщениях по тикету (рассылка + карточки)."""
        kb = _decided_keyboard(label)
        for m in store.get_ticket_messages(ticket_id):
            self._set_keyboard(m["chat_id"], m["message_id"], kb)
        store.clear_ticket_messages(ticket_id)

    def _reply(self, chat_id, text: str, reply_to=None, markup: dict | None = None) -> None:
        try:
            self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=markup,
                reply_to_message_id=reply_to,
            )
        except TelegramError as e:
            log.warning("send_message: %s", e)

    def _ack(self, callback_query_id: str, text: str | None = None) -> None:
        try:
            self.bot.answer_callback_query(callback_query_id, text)
        except TelegramError as e:
            log.warning("answerCallbackQuery: %s", e)

    def _set_keyboard(self, chat_id, message_id, markup: dict) -> None:
        if chat_id is None or message_id is None:
            return
        try:
            self.bot.edit_message_reply_markup(chat_id, message_id, markup)
        except TelegramError as e:
            log.warning("editMessageReplyMarkup: %s", e)

    def dispatch(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                self.handle_callback(update["callback_query"])
            elif "message" in update:
                self.handle_message(update["message"])
        except Exception:
            log.exception("ошибка обработки апдейта %s", update.get("update_id"))

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        if not config.telegram.chat_id:
            log.warning("TELEGRAM_CHAT_ID не задан — рассылка работать не будет")
        log.info("ReviewBot запущен, начинаю long-polling")
        offset: int | None = None
        poll = config.telegram.poll_timeout
        while True:
            try:
                updates = self.bot.get_updates(offset=offset, timeout=poll)
            except Exception:
                log.exception("getUpdates упал, пауза 5с")
                time.sleep(5)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                self.dispatch(upd)


def main() -> int:
    ReviewBot().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
