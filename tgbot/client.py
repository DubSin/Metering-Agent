"""
Тонкий sync-клиент Telegram Bot API на httpx.

Реализованы только методы, нужные для ревью тикетов:
  send_message, edit_message_reply_markup, answer_callback_query, get_updates.

Имя пакета `tgbot` (а не `telegram`) специально: библиотека python-telegram-bot
импортируется как `telegram` и затёрла бы наш модуль.
"""
from __future__ import annotations

import logging

import httpx

from config import config

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    """Telegram API вернул ok=false."""


class BotClient:
    def __init__(
        self,
        token: str | None = None,
        api_base: str | None = None,
        proxy: str | None = None,
    ) -> None:
        self.token = token or config.telegram.bot_token
        if not self.token:
            raise ValueError("Не задан TELEGRAM_BOT_TOKEN")
        base = (api_base or config.telegram.api_base).rstrip("/")
        self._base_url = f"{base}/bot{self.token}"
        # Прокси только для ТГ; пусто → прямое соединение.
        self._proxy = (proxy if proxy is not None else config.telegram.proxy) or None

    def _call(self, method: str, payload: dict, timeout: float = 30.0) -> dict:
        url = f"{self._base_url}/{method}"
        resp = httpx.post(url, json=payload, timeout=timeout, proxy=self._proxy)
        # Telegram кладёт причину в тело ({"ok":false,"description":...}) —
        # читаем его и при ошибке, иначе raise_for_status его теряет.
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not resp.is_success or not (data and data.get("ok")):
            desc = (data or {}).get("description") if data else resp.text.strip()
            raise TelegramError(f"{method}: HTTP {resp.status_code} — {desc or '(пусто)'}")
        return data.get("result")

    # ------------------------------------------------------------------ #
    def send_message(
        self,
        chat_id: str | int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = "HTML",
        disable_web_page_preview: bool = True,
        reply_to_message_id: int | None = None,
    ) -> dict:
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        return self._call("sendMessage", payload)

    def edit_message_reply_markup(
        self,
        chat_id: str | int,
        message_id: int,
        reply_markup: dict | None = None,
    ) -> dict:
        payload: dict = {"chat_id": chat_id, "message_id": message_id}
        # Передаём пустую клавиатуру, чтобы убрать кнопки
        payload["reply_markup"] = reply_markup or {"inline_keyboard": []}
        return self._call("editMessageReplyMarkup", payload)

    def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> dict:
        payload: dict = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._call("answerCallbackQuery", payload)

    def get_updates(
        self, offset: int | None = None, timeout: int = 30
    ) -> list[dict]:
        payload: dict = {
            "timeout": timeout,
            "allowed_updates": ["callback_query", "message"],
        }
        if offset is not None:
            payload["offset"] = offset
        # http-таймаут должен быть больше long-poll таймаута
        return self._call("getUpdates", payload, timeout=timeout + 10) or []
