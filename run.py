#!/usr/bin/env python3
"""
Запуск всего пайплайна одной командой.

Поднимает в одном процессе:
  • Telegram-бот ревью (long-polling) — в фоновом потоке;
  • поллер HelpDesk — каждые POLL_INTERVAL секунд сам тянет новые тикеты
    (только GET) и шлёт их на ревью в Telegram (главный поток).

RAG-пайплайн (эмбеддинги/Qdrant) инициализируется один раз при старте и общий
для обоих (singleton в processing.py).

Запуск:
    python run.py                  # бот + поллер
    python run.py --no-poller      # только бот (ревью вручную через /fetch)
    python run.py --no-bot         # только поллер

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
    ap.add_argument("--no-bot", action="store_true", help="не запускать Telegram-бота")
    ap.add_argument("--no-poller", action="store_true", help="не запускать поллер HelpDesk")
    args = ap.parse_args()

    if args.no_bot and args.no_poller:
        log.error("Нечего запускать: указаны и --no-bot, и --no-poller")
        return 1

    # Прогреваем RAG-пайплайн один раз до старта потоков: грузим эмбеддер/Qdrant
    # заранее (быстрый первый ответ) и избегаем гонки за ленивую инициализацию
    # между поллером и ботом.
    try:
        from processing import get_pipeline

        log.info("Инициализирую RAG-пайплайн (эмбеддинги/Qdrant)…")
        get_pipeline()
    except Exception:
        log.exception("Не удалось прогреть RAG-пайплайн (продолжаю, поднимется лениво)")

    bot_thread: threading.Thread | None = None
    if not args.no_bot:
        log.info("Запускаю Telegram-бота (long-polling)…")
        bot_thread = threading.Thread(target=_start_bot, name="tgbot", daemon=True)
        bot_thread.start()

    if not args.no_poller:
        from poller import run as run_poller

        stop = threading.Event()
        try:
            run_poller(stop)  # блокирует главный поток
        except KeyboardInterrupt:
            log.info("Остановлено пользователем")
            stop.set()
    elif bot_thread is not None:
        # только бот: держим главный поток живым
        try:
            bot_thread.join()
        except KeyboardInterrupt:
            log.info("Остановлено пользователем")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
