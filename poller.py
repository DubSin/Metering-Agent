"""
Поллер HelpDesk: периодически сам тянет новые тикеты (только GET) и шлёт их
на ревью в Telegram. Замена push-webhook.

Зачем pull, а не push: HelpDesk у нас — внешний SaaS (helpdeskeddy.com), а Mac
стоит за NAT/корпоративным VPN, и достучаться до него извне облако не может.
Поэтому не ждём событий, а сами опрашиваем HelpDesk каждые POLL_INTERVAL секунд.

Логика приёма — общая с командой /fetch (см. processing.fetch_and_review):
текст тикета → RAG-инструкция → рассылка в Telegram-группу с кнопками
Approve/Decline/Your answer. Дедупликация уже отправленных тикетов — внутри
fetch_and_review через store.exists(), повторно один тикет не рассылается.

Запуск обычно в потоке из run.py; для отладки можно отдельно:
    python -m poller            # бесконечный цикл с интервалом из config
    python -m poller --once     # один проход и выход
"""
from __future__ import annotations

import argparse
import logging
import threading
import time

from config import config
from processing import process_new_tickets

log = logging.getLogger("poller")


def poll_once() -> dict:
    """Один проход опроса: подтянуть ТОЛЬКО новые тикеты и отправить на ревью.

    Новизна определяется по водяному знаку (id последнего обработанного тикета),
    поэтому старые открытые тикеты повторно не тянутся — только появившиеся после.
    Возвращает сводку process_new_tickets: {found, sent, skipped, empty, watermark}.
    """
    t_start = time.perf_counter()
    summary = process_new_tickets()
    elapsed = time.perf_counter() - t_start
    if summary.get("initialized"):
        log.info(
            "поллер: первый проход за %.2fc, водяной знак=%s — бэклог пропущен",
            elapsed,
            summary.get("watermark"),
        )
    elif summary.get("sent"):
        log.info(
            "поллер: проход за %.2fc — найдено=%s, отправлено=%s, пропущено=%s, пусто=%s, знак=%s",
            elapsed,
            summary.get("found"),
            len(summary["sent"]),
            len(summary.get("skipped", [])),
            len(summary.get("empty", [])),
            summary.get("watermark"),
        )
    else:
        log.debug(
            "поллер: новых тикетов нет за %.2fc (найдено=%s)",
            elapsed,
            summary.get("found"),
        )
    return summary


def run(stop: threading.Event | None = None) -> None:
    """Бесконечный цикл опроса с интервалом config.poll_interval (секунды).

    stop — необязательное событие для корректной остановки: цикл прерывается
    между проходами, не дожидаясь полного интервала. Падение одного прохода
    логируется и не останавливает поллер.
    """
    interval = config.poll_interval
    log.info(
        "Поллер запущен: интервал %s c, статусы=%s, лимит=%s",
        interval,
        config.fetch_statuses,
        config.fetch_limit,
    )
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            poll_once()
        except Exception:
            log.exception("поллер: проход упал, продолжаю по расписанию")
        stop.wait(interval)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    args = ap.parse_args()
    if args.once:
        poll_once()
    else:
        run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
