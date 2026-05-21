"""
Инструменты работы с HelpDeskEddy API v2.

Документация: https://helpdeskeddy.ru/api.html

Аутентификация:
    Authorization: Basic base64(email:api_key)

Базовый URL:
    {host}/api/v2/...

Ключевые эндпоинты, которыми пользуется агент:
    GET    /tickets/                    — список заявок (с фильтрами)
    GET    /tickets/{id}                — метаданные одной заявки
    GET    /tickets/{id}/posts/         — публичные сообщения (виден клиенту)
    POST   /tickets/{id}/posts/         — добавить публичный ответ
    POST   /tickets/{id}/comments/      — добавить внутренний комментарий
    PUT    /tickets/{id}                — изменить статус/исполнителя/приоритет

По проектному решению агент отвечает ВНУТРЕННИМ комментарием
(`config.helpdesk_eddy.reply_channel == "comments"`) и после успешной отправки
переводит тикет в статус `process` (`post_reply_status`).
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import tool

from config import config

log = logging.getLogger(__name__)

API_PREFIX = "/api/v2"


def _auth_header() -> dict[str, str]:
    """Basic auth по схеме HelpDeskEddy: base64(email:api_key).

    Учётка — почта пользователя HDE, ключ — из его профиля
    (Управление → Глобальные настройки → Система для администратора).
    """
    cfg = config.helpdesk_eddy
    if not cfg.email or not cfg.api_key:
        raise RuntimeError(
            "HelpDeskEddy credentials are not set: "
            "задайте HELPDESK_EDDY_EMAIL и HELPDESK_EDDY_API_KEY"
        )
    token = base64.b64encode(f"{cfg.email}:{cfg.api_key}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _client() -> httpx.AsyncClient:
    cfg = config.helpdesk_eddy
    return httpx.AsyncClient(
        base_url=cfg.base_url.rstrip("/") + API_PREFIX,
        headers={**_auth_header(), "Accept": "application/json"},
        timeout=cfg.request_timeout,
    )


def _unwrap(payload: Any) -> Any:
    """HDE оборачивает успешные ответы в {"data": ...}. Достаём содержимое."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _hde_error(payload: Any) -> str | None:
    """Достаёт первую ошибку из формата HDE: {"errors":[{code,title,details}]}."""
    if isinstance(payload, dict) and payload.get("errors"):
        e = payload["errors"][0]
        return f"{e.get('code')}: {e.get('title')} — {e.get('details')}"
    return None


# ---------- чтение ----------

@tool
async def get_task_details(task_id: str) -> dict:
    """Получить метаданные заявки HelpDeskEddy и текст первого сообщения.

    Возвращает {task_id, subject, text, author, created_at, status_id, attachments, raw}.
    Текст берём из самого раннего поста (`GET /tickets/{id}/posts/`), т.к.
    `GET /tickets/{id}` возвращает только метаданные.
    """
    try:
        async with _client() as c:
            r_meta = await c.get(f"/tickets/{task_id}")
            meta_json = r_meta.json() if r_meta.content else {}
            err = _hde_error(meta_json)
            if err:
                return {"task_id": task_id, "status_code": r_meta.status_code, "error": err}
            r_meta.raise_for_status()
            ticket = _unwrap(meta_json) or {}

            # Тянем первый пост = тело заявки.
            r_posts = await c.get(
                f"/tickets/{task_id}/posts/",
                params={"page": 1, "order_by": "date_created"},
            )
            posts_json = r_posts.json() if r_posts.content else {}
            err = _hde_error(posts_json)
            if err:
                return {"task_id": task_id, "status_code": r_posts.status_code, "error": err}
            r_posts.raise_for_status()
            posts = _unwrap(posts_json) or []
            if isinstance(posts, dict):
                # HDE отдаёт {post_id: {...}} — превратим в list, сортировку
                # обеспечивает API параметром order_by.
                posts = [v for v in posts.values() if isinstance(v, dict)]
            first = posts[0] if isinstance(posts, list) and posts else {}

            text = first.get("text") or first.get("message") or ""
            attachments = first.get("files") or first.get("attachments") or []

            return {
                "task_id": str(ticket.get("id") or task_id),
                "unique_id": ticket.get("unique_id"),
                "subject": ticket.get("title") or ticket.get("subject"),
                "text": text,
                "author": (first.get("user_id") or ticket.get("user_id")),
                "created_at": ticket.get("date_created"),
                "status_id": ticket.get("status_id"),
                "priority_id": ticket.get("priority_id"),
                "department_id": ticket.get("department_id"),
                "attachments": attachments,
                "raw": ticket,
            }
    except httpx.HTTPError as e:
        log.exception("get_task_details failed: task=%s", task_id)
        return {"task_id": task_id, "error": str(e)}


@tool
async def list_new_tickets(since: str | None = None, limit: int = 50) -> list[dict]:
    """Получить список заявок из HelpDeskEddy для агента.

    Используется фоновым поллером — страховка на случай пропущенного webhook.

    `since` — нижняя граница времени создания в формате `YYYY-MM-DD HH:MM:SS`
    (передаётся как `from_date_created`). Если `None`, ограничения по дате нет.
    Фильтр по статусам — из `config.helpdesk_eddy.poll_status_list` (по
    умолчанию `open`). Сортировка — по дате создания.

    Возвращает массив словарей с метаданными (без текста — текст подтягивает
    `get_task_details` дальше по графу).
    """
    cfg = config.helpdesk_eddy
    params: dict[str, str | int] = {
        "page": 1,
        "status_list": cfg.poll_status_list,
        "order_by": "date_created",
    }
    if since:
        params["from_date_created"] = since

    try:
        async with _client() as c:
            r = await c.get("/tickets/", params=params)
            data_json = r.json()
            err = _hde_error(data_json)
            if err:
                return [{"error": err}]
            r.raise_for_status()
            items = _unwrap(data_json) or []
            if isinstance(items, dict):
                # HDE отдаёт data как {ticket_id: {...ticket}}, а не как list.
                # Прочие варианты на всякий случай: {tickets:[...]} / {result:[...]}.
                if "tickets" in items or "result" in items:
                    items = items.get("tickets") or items.get("result") or []
                else:
                    items = [v for v in items.values() if isinstance(v, dict)]
            if not isinstance(items, list):
                items = []

            out: list[dict] = []
            for it in items[:limit]:
                if not isinstance(it, dict):
                    continue
                out.append({
                    "task_id": str(it.get("id")),
                    "id": it.get("id"),
                    "unique_id": it.get("unique_id"),
                    "subject": it.get("title") or it.get("subject"),
                    "text": "",  # тянем отдельно в get_task_details
                    "created_at": it.get("date_created"),
                    "updated_at": it.get("date_updated"),
                    "status_id": it.get("status_id"),
                    "department_id": it.get("department_id"),
                })
            return out
    except httpx.HTTPError as e:
        log.exception("list_new_tickets failed")
        return [{"error": str(e)}]


# ---------- запись ----------

async def _set_status(client: httpx.AsyncClient, task_id: str, status_id: str) -> dict:
    """PUT /tickets/{id} — внутренняя помощь, не tool."""
    try:
        r = await client.put(f"/tickets/{task_id}", json={"status_id": status_id})
        body = r.json() if r.content else {}
        err = _hde_error(body)
        if err:
            return {"ok": False, "error": err}
        r.raise_for_status()
        return {"ok": True, "data": _unwrap(body)}
    except httpx.HTTPError as e:
        log.exception("set_status failed: task=%s status=%s", task_id, status_id)
        return {"ok": False, "error": str(e)}


@tool
async def reply_to_task(
    task_id: str,
    text: str,
    attachments: list[str] | None = None,
) -> dict:
    """Отправить ответ агента в заявку HelpDeskEddy.

    Канал ответа выбирается через `config.helpdesk_eddy.reply_channel`:
      - "comments" — внутренний комментарий (видят только сотрудники) — ДЕФОЛТ
      - "posts"    — публичный пост (видит клиент)

    После успешной отправки переводит тикет в `post_reply_status`
    (по умолчанию `process`), если он задан.

    `attachments` — список абсолютных путей к локальным файлам (xlsx/png).
    Передаются через multipart как `files` (по доке HDE).
    """
    cfg = config.helpdesk_eddy
    channel = (cfg.reply_channel or "comments").strip()
    if channel not in ("comments", "posts"):
        log.warning("unknown reply_channel=%r, falling back to comments", channel)
        channel = "comments"
    url = f"/tickets/{task_id}/{channel}/"

    files = []
    for p in attachments or []:
        path = Path(p)
        if not path.exists():
            log.warning("attachment not found, skip: %s", p)
            continue
        files.append(("files", (path.name, path.read_bytes())))

    try:
        async with _client() as c:
            if files:
                r = await c.post(url, data={"text": text}, files=files)
            else:
                # без файлов — обычный form-encoded POST
                r = await c.post(url, data={"text": text})
            body = r.json() if r.content else {}
            err = _hde_error(body)
            if err:
                return {"task_id": task_id, "channel": channel, "error": err}
            r.raise_for_status()
            result: dict = {
                "task_id": task_id,
                "channel": channel,
                "data": _unwrap(body),
            }

            # Смена статуса после успешного ответа.
            if cfg.post_reply_status:
                status_res = await _set_status(c, task_id, cfg.post_reply_status)
                result["status_change"] = status_res
                if not status_res.get("ok"):
                    log.warning(
                        "reply ok, but status change to %s failed: %s",
                        cfg.post_reply_status, status_res.get("error"),
                    )

            return result
    except httpx.HTTPError as e:
        log.exception("reply_to_task failed: task=%s", task_id)
        return {"task_id": task_id, "channel": channel, "error": str(e)}


@tool
async def update_ticket_status(task_id: str, status_id: str) -> dict:
    """Изменить статус заявки HelpDeskEddy (open / process / closed)."""
    try:
        async with _client() as c:
            res = await _set_status(c, task_id, status_id)
            return {"task_id": task_id, **res}
    except httpx.HTTPError as e:
        return {"task_id": task_id, "ok": False, "error": str(e)}
