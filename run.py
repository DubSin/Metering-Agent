#!/usr/bin/env python3
"""
Запуск всего пайплайна одной командой.

Поднимает в одном процессе:
  • Telegram-бот ревью (long-polling) — в фоновом потоке;
  • webhook-приёмник HelpDesk (uvicorn) — в главном потоке.

RAG-пайплайн (эмбеддинги/Qdrant) при этом инициализируется один раз и общий
для обоих (singleton в processing.py).

Запуск:
    python run.py                       # бот + webhook на 0.0.0.0:8080
    python run.py --port 9000           # другой порт
    python run.py --no-webhook          # только бот (например, для /fetch без проброса)
    python run.py --no-bot              # только webhook

Ctrl+C — корректно завершает оба.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("run")


def _start_bot() -> None:
    try:
        from tgbot.bot import ReviewBot

        ReviewBot().run()
    except Exception:
        log.exception("Telegram-бот остановлен из-за ошибки")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-bot", action="store_true", help="не запускать Telegram-бота")
    ap.add_argument("--no-webhook", action="store_true", help="не запускать webhook")
    args = ap.parse_args()

    if args.no_bot and args.no_webhook:
        log.error("Нечего запускать: указаны и --no-bot, и --no-webhook")
        return 1

    bot_thread: threading.Thread | None = None
    if not args.no_bot:
        log.info("Запускаю Telegram-бота (long-polling)…")
        bot_thread = threading.Thread(target=_start_bot, name="tgbot", daemon=True)
        bot_thread.start()

    if not args.no_webhook:
        import uvicorn

        log.info("Запускаю webhook на %s:%s …", args.host, args.port)
        uvicorn.run("webhook:app", host=args.host, port=args.port, log_level="info")
    elif bot_thread is not None:
        # только бот: держим главный поток живым
        try:
            bot_thread.join()
        except KeyboardInterrupt:
            log.info("Остановлено пользователем")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
