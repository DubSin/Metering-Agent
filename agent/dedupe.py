"""
Двухуровневый дедуп тикетов HelpDeskEddy.

Назначение — гарантировать, что один тикет обработается ровно один раз,
даже если он прилетел и через webhook, и через polling, и сам процесс
успел упасть посередине.

Уровни:
  - in-memory lock (`_locked`): пока агент обрабатывает тикет, повторные
    попытки взять его в работу из другого канала или из того же поллинг-цикла
    отклоняются. Лок снимается при `commit()` или `release()`.
  - on-disk commit (`_committed_path`): сюда тикет попадает ТОЛЬКО после
    успешной отправки ответа в HelpDeskEddy. Если процесс рестартанул в
    середине обработки — тикет «забыт» (его нет в файле), и при следующем
    тике поллера он будет обработан заново. Это лучше, чем «помечен — но
    клиент ничего не получил».

Интерфейс:
  - try_lock(id)  → True, если удалось забронировать (раньше не видели).
                     False, если уже залочен или уже закоммичен.
  - commit(id)    → пометить успешно обработанным (запись на диск + снять лок).
  - release(id)   → откатить лок при сбое, БЕЗ записи на диск.

Если приложение работает в несколько uvicorn-воркеров, in-memory лок
живёт в каждом процессе свой — это deferred (нужен SQLite/Redis).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from config import config

log = logging.getLogger(__name__)

_lock = asyncio.Lock()
_locked: set[str] = set()
_committed: set[str] | None = None  # ленивая инициализация из файла


def _committed_path() -> Path:
    p = Path(config.processed_tickets_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.touch()
    return p


def _load_committed() -> set[str]:
    global _committed
    if _committed is None:
        p = _committed_path()
        _committed = {
            line.strip()
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        log.info("dedupe: loaded %d committed ticket(s)", len(_committed))
    return _committed


async def try_lock(task_id: str) -> bool:
    """Забронировать тикет в обработку. False, если он уже идёт или закоммичен."""
    async with _lock:
        committed = _load_committed()
        if task_id in committed or task_id in _locked:
            return False
        _locked.add(task_id)
        return True


async def commit(task_id: str) -> None:
    """Пометить тикет успешно обработанным (запись на диск, снять in-memory lock)."""
    async with _lock:
        committed = _load_committed()
        if task_id not in committed:
            with _committed_path().open("a", encoding="utf-8") as f:
                f.write(f"{task_id}\n")
            committed.add(task_id)
        _locked.discard(task_id)
        log.debug("dedupe: committed %s", task_id)


async def release(task_id: str) -> None:
    """Снять in-memory lock без записи на диск (при сбое обработки)."""
    async with _lock:
        _locked.discard(task_id)
        log.debug("dedupe: released %s (will retry later)", task_id)


async def is_committed(task_id: str) -> bool:
    async with _lock:
        return task_id in _load_committed()
