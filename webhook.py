"""
FastAPI webhook-приёмник от HelpDesk.

Запуск:
    uvicorn webhook:app --host 0.0.0.0 --port 8080

Ожидаемый формат payload (адаптируй под свою HelpDesk при подключении):
    {
      "task_id": "12345",
      "text": "Текст обращения",
      "author": "...",
      "attachments": [...]   # опционально
    }
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel

from agent import run_task
from config import config

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
    return {"ok": True, "model": config.openai_model}


@app.post("/helpdesk/webhook")
async def helpdesk_webhook(
    payload: HelpDeskWebhook,
    background: BackgroundTasks,
    x_helpdesk_signature: str | None = Header(default=None),
) -> dict:
    # TODO(webhook_auth): проверить подпись/HMAC, если HelpDesk её шлёт.
    if not payload.task_id:
        raise HTTPException(status_code=400, detail="task_id is required")

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
