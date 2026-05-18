"""
Простейший файловый дедуп для тикетов HelpDeskEddy.

Защищает от двойной обработки одного тикета, если он пришёл одновременно
через webhook и через polling. Хранится плоский список ID в одну строку.

Когда захочется надёжности (рестарт без race, очистка старых) — заменить
на SQLite/Redis, не трогая интерфейс mark/seen.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config import config

log = logging.getLogger(__name__)

_lock = asyncio.Lock()


def _path() -> Path:
    p = Path(config.processed_tickets_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.touch()
    return p


async def seen(task_id: str) -> bool:
    """Был ли уже обработан тикет."""
    async with _lock:
        p = _path()
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip() == task_id:
                return True
        return False


async def mark(task_id: str) -> None:
    """Пометить тикет как обработанный."""
    async with _lock:
        p = _path()
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{task_id}\n")
        log.debug("dedupe: marked %s", task_id)


async def acquire(task_id: str) -> bool:
    """Атомарно проверить и пометить. True — можно обрабатывать, False — дубль."""
    async with _lock:
        p = _path()
        existing = set(p.read_text(encoding="utf-8").splitlines())
        if task_id in existing:
            return False
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{task_id}\n")
        return True
