"""
FastAPI приёмник заявок HelpDeskEddy.

Запуск:
    uvicorn webhook:app --host 0.0.0.0 --port 8080

Два канала поступления тикетов — оба идут через один и тот же граф:

  1. Webhook (push). HelpDeskEddy дёргает POST /helpdesk/webhook при создании
     тикета (раздел «Исходящий канал» в админке). Реальное время.
     Формат исходящего payload в API-доке не зафиксирован — поэтому здесь
     принимаем произвольный JSON и достаём ID заявки из самых ожидаемых полей
     (`ticket_id`, `id`, `data.ticket.id`, ...).

  2. Поллинг (pull). При старте приложения мы стартуем фоновую задачу, которая
     раз в `config.poll_interval_seconds` запрашивает у HelpDeskEddy свежие
     тикеты со статусом `open` (см. `list_new_tickets`). Курсор сохраняется
     в файле в формате `YYYY-MM-DD HH:MM:SS` (это `from_date_created` HDE).
     Дубли отсекаются файловым дедупом (`agent/dedupe.py`).

Защита от дублей обязательна: один и тот же тикет может прилететь по обоим
каналам почти одновременно.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

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


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": config.ergpt_model}


# ---------- Webhook (push-канал) ----------

def _extract_task_id(payload: Any) -> str | None:
    """Достаём ID заявки из произвольной структуры HDE-вебхука.

    Формат «Исходящего канала» в публичной доке не описан, поэтому пробуем
    самые вероятные пути. При первом тестовом запуске стоит залогировать
    реальный payload и при необходимости расширить список.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("ticket_id", "task_id", "id"):
        v = payload.get(key)
        if v:
            return str(v)
    # вложенные варианты
    ticket = payload.get("ticket")
    if isinstance(ticket, dict):
        v = ticket.get("id") or ticket.get("ticket_id")
        if v:
            return str(v)
    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_task_id(data)
    return None


def _verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 от raw body, hex. Включается только если задан секрет."""
    if not secret:
        return True
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    # допускаем префикс `sha256=` если HDE так шлёт
    sig = signature.split("=", 1)[1] if "=" in signature else signature
    return hmac.compare_digest(expected, sig.strip())


@app.post("/helpdesk/webhook")
async def helpdesk_webhook(
    request: Request,
    background: BackgroundTasks,
    x_helpdesk_signature: str | None = Header(default=None),
) -> dict:
    body = await request.body()

    if not _verify_signature(
        config.helpdesk_eddy.webhook_secret, body, x_helpdesk_signature
    ):
        log.warning("webhook signature mismatch")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    task_id = _extract_task_id(payload)
    if not task_id:
        log.warning("webhook payload without ticket id: %r", payload)
        raise HTTPException(status_code=400, detail="ticket id not found in payload")

    if not await acquire(task_id):
        log.info("webhook: dup task_id=%s — skip", task_id)
        return {"accepted": False, "duplicate": True, "task_id": task_id}

    log.info("webhook accepted: task_id=%s", task_id)

    # Текст заявки тянем уже в графе через get_task_details — webhook от HDE
    # не гарантирует, что тело сообщения придёт в payload.
    async def _run() -> None:
        try:
            await run_task(task_id=task_id, raw_text="", raw_payload=payload)
        except Exception:
            log.exception("task pipeline failed: %s", task_id)

    background.add_task(lambda: asyncio.create_task(_run()))
    return {"accepted": True, "task_id": task_id}


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
        # первый запуск без тикетов — сохраняем «сейчас» в HDE-формате,
        # чтобы не тянуть всю историю.
        _save_cursor(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

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
