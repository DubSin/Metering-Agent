"""
FastAPI приёмник заявок HelpDeskEddy.

Запуск:
    uvicorn webhook:app --host 0.0.0.0 --port 8080

Два канала поступления тикетов — оба идут через один и тот же граф:

  1. Webhook (push). HelpDeskEddy дёргает POST /helpdesk/webhook при создании
     тикета. Реальное время, но может потеряться при сбоях сети/HD.

  2. Поллинг (pull). При старте приложения мы стартуем фоновую задачу, которая
     раз в `config.poll_interval_seconds` запрашивает у HelpDeskEddy свежие
     тикеты (см. `list_new_tickets`). Курсор сохраняется в файле, дубли
     отсекаются файловым дедупом (`agent/dedupe.py`).

Защита от дублей обязательна: один и тот же тикет может прилететь по обоим
каналам почти одновременно.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel

from agent import run_task
from agent.dedupe import acquire, seen
from config import config
from tools import list_new_tickets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("webhook")

app = FastAPI(title="Metering TP Agent")


class HelpDeskWebhook(BaseModel):
    task_id: str
    text: str | None = ""
    author: str | None = None
    attachments: list[Any] | None = None


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": config.ergpt_model}


# ---------- Webhook (push-канал) ----------

@app.post("/helpdesk/webhook")
async def helpdesk_webhook(
    payload: HelpDeskWebhook,
    background: BackgroundTasks,
    x_helpdesk_signature: str | None = Header(default=None),
) -> dict:
    # TODO(webhook_auth): проверить подпись/HMAC, если HelpDeskEddy её шлёт.
    if not payload.task_id:
        raise HTTPException(status_code=400, detail="task_id is required")

    if not await acquire(payload.task_id):
        log.info("webhook: dup task_id=%s — skip", payload.task_id)
        return {"accepted": False, "duplicate": True, "task_id": payload.task_id}

    log.info("webhook accepted: task_id=%s", payload.task_id)

    async def _run() -> None:
        try:
            await run_task(
                task_id=payload.task_id,
                raw_text=payload.text or "",
                raw_payload=payload.model_dump(),
            )
        except Exception:
            log.exception("task pipeline failed: %s", payload.task_id)

    background.add_task(lambda: asyncio.create_task(_run()))
    return {"accepted": True, "task_id": payload.task_id}


# ---------- Поллер (pull-канал) ----------

def _cursor_path() -> Path:
    p = Path(config.poll_cursor_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_cursor() -> str | None:
    p = _cursor_path()
    if not p.exists():
        return None
    val = p.read_text(encoding="utf-8").strip()
    return val or None


def _save_cursor(value: str) -> None:
    _cursor_path().write_text(value, encoding="utf-8")


async def _poll_once() -> int:
    """Один цикл опроса HelpDeskEddy. Возвращает кол-во запущенных тикетов."""
    since = _load_cursor()
    log.debug("poll: since=%s", since)
    items = await list_new_tickets.ainvoke({"since": since, "limit": 50})

    started = 0
    newest_ts: str | None = None
    for it in items:
        if not isinstance(it, dict) or "error" in it:
            log.warning("poll: bad item: %r", it)
            continue
        tid = str(it.get("task_id") or it.get("id") or "")
        if not tid:
            continue
        ts = it.get("created_at") or it.get("updated_at")
        if ts and (newest_ts is None or ts > newest_ts):
            newest_ts = ts

        if await seen(tid):
            continue
        if not await acquire(tid):
            continue

        text = it.get("text") or it.get("subject") or ""

        async def _run(task_id: str = tid, raw_text: str = text, payload: dict = it) -> None:
            try:
                await run_task(task_id=task_id, raw_text=raw_text, raw_payload=payload)
            except Exception:
                log.exception("poll: task pipeline failed: %s", task_id)

        asyncio.create_task(_run())
        started += 1

    if newest_ts:
        _save_cursor(newest_ts)
    elif since is None:
        # первый запуск без тикетов — сохраняем «сейчас», чтобы не тянуть всю историю
        _save_cursor(datetime.now(timezone.utc).isoformat())

    if started:
        log.info("poll: started %d new task(s)", started)
    return started


async def _poll_loop() -> None:
    log.info("poller: started, interval=%ss", config.poll_interval_seconds)
    while True:
        try:
            await _poll_once()
        except Exception:
            log.exception("poll cycle failed")
        await asyncio.sleep(config.poll_interval_seconds)


@app.on_event("startup")
async def _on_startup() -> None:
    asyncio.create_task(_poll_loop())


@app.post("/poll")
async def poll_now() -> dict:
    """Ручной триггер поллера (для отладки и cron-вариантов)."""
    started = await _poll_once()
    return {"started": started}
