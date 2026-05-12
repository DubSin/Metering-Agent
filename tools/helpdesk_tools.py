"""
Инструменты работы с HelpDesk: чтение задачи и отправка ответа с вложениями.

Структура webhook/полей зависит от конкретной HelpDesk — оставлен generic-формат,
который ожидает {task_id, author, subject, text, attachments}. Адаптер
к конкретной системе — TODO(helpdesk_adapter).
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx
from langchain_core.tools import tool

from config import config

log = logging.getLogger(__name__)


def _client() -> httpx.AsyncClient:
    headers = {}
    if config.helpdesk.api_key:
        headers["Authorization"] = f"Bearer {config.helpdesk.api_key}"
    return httpx.AsyncClient(
        base_url=config.helpdesk.base_url,
        headers=headers,
        timeout=30,
    )


@tool
async def get_task_details(task_id: str) -> dict:
    """Получить текст и метаданные задачи HelpDesk по ID.

    Возвращает {task_id, subject, text, author, created_at, attachments: [...]}.
    """
    # TODO(endpoint): путь чтения задачи в HelpDesk
    url = f"/api/v1/tasks/{task_id}"
    try:
        async with _client() as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPError as e:
        log.exception("get_task_details failed: task=%s", task_id)
        return {"task_id": task_id, "error": str(e)}


@tool
async def reply_to_task(
    task_id: str,
    text: str,
    attachments: list[str] | None = None,
) -> dict:
    """Отправить ответ клиенту по задаче HelpDesk.

    attachments — список абсолютных путей к локальным файлам (xlsx/png).
    Возвращает результат API HelpDesk либо {error}.
    """
    # TODO(endpoint): путь отправки комментария/ответа в HelpDesk
    url = f"/api/v1/tasks/{task_id}/reply"
    files = []
    for p in attachments or []:
        path = Path(p)
        if not path.exists():
            log.warning("attachment not found, skip: %s", p)
            continue
        files.append(("attachments", (path.name, path.read_bytes())))

    try:
        async with _client() as c:
            if files:
                r = await c.post(url, data={"text": text}, files=files)
            else:
                r = await c.post(url, json={"text": text})
            r.raise_for_status()
            return r.json()
    except httpx.HTTPError as e:
        log.exception("reply_to_task failed: task=%s", task_id)
        return {"task_id": task_id, "error": str(e)}
