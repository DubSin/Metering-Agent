"""
Чтение задач HelpDeskEddy (API v2, Basic auth email:api_key).

В пайплайне обработки тикета к HelpDesk идут ТОЛЬКО GET-запросы — ответы клиенту
оператор отправляет сам через ссылку на тикет, поэтому writer-инструментов здесь нет.

Sync-функции (`get_ticket_text`, `make_client`, ...) переиспользуются скриптами
(`scripts.process_tickets`) и webhook'ом. Для совместимости с LangGraph оставлена
async-обёртка-инструмент `get_task_details`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import tool

from config import config

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# HelpDeskEddy API v2 клиент (sync)
# --------------------------------------------------------------------------- #
def api_base() -> str:
    """База API v2 из HELPDESK_EDDY_BASE_URL (только схема и хост)."""
    parts = urlsplit(config.helpdesk_eddy.base_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(
            f"HELPDESK_EDDY_BASE_URL задан некорректно: {config.helpdesk_eddy.base_url!r}"
        )
    return f"{parts.scheme}://{parts.netloc}/api/v2"


def make_client() -> httpx.Client:
    """httpx-клиент с Basic-аутентификацией (email:api_key)."""
    email = config.helpdesk_eddy.email
    api_key = config.helpdesk_eddy.api_key
    if not email or not api_key:
        raise ValueError(
            "Нужны HELPDESK_EDDY_EMAIL и HELPDESK_EDDY_API_KEY "
            "(Basic-аутентификация API v2)."
        )
    return httpx.Client(
        base_url=api_base(),
        auth=(email, api_key),
        headers={"Accept": "application/json"},
        timeout=60,
        follow_redirects=True,
    )


def paginate(
    client: httpx.Client,
    path: str,
    params: dict,
    limit: int | None = None,
) -> list[dict]:
    """Собрать объекты со всех страниц. data приходит словарём {id: obj} или списком."""
    items: list[dict] = []
    page = 1
    while True:
        resp = client.get(path, params={**params, "page": page})
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or {}
        if isinstance(data, dict):
            items.extend(data.values())
        elif isinstance(data, list):
            items.extend(data)

        if limit and len(items) >= limit:
            return items[:limit]

        pg = payload.get("pagination") or {}
        total_pages = pg.get("total_pages")
        current = pg.get("current_page", page)
        if not total_pages or current >= total_pages:
            break
        page = current + 1
    return items


# --------------------------------------------------------------------------- #
# Извлечение текста тикета
# --------------------------------------------------------------------------- #
def strip_html(html: str) -> str:
    """Убрать разметку из тела поста (тот же подход, что в rag.kb_loader)."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _pick(d: dict, *keys: str) -> str:
    """Первое непустое строковое значение по списку возможных ключей."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def ticket_subject(ticket: dict) -> str:
    return _pick(ticket, "subject", "title", "name")


def ticket_body(client: httpx.Client, ticket_id: str | int) -> str:
    """Тело обращения = первый пост тикета (GET /tickets/{id}/posts/)."""
    try:
        posts = paginate(client, f"/tickets/{ticket_id}/posts/", {})
    except httpx.HTTPError as e:
        log.warning("не удалось прочитать посты тикета %s: %s", ticket_id, e)
        return ""
    if not posts:
        return ""
    posts.sort(key=lambda p: p.get("date_created") or p.get("id") or 0)
    first = posts[0]
    return strip_html(_pick(first, "message", "text", "body", "content"))


def ticket_text(client: httpx.Client, ticket: dict) -> str:
    """Склеить тему и тело обращения в один текст (для RAG)."""
    subject = ticket_subject(ticket)
    body = ticket_body(client, ticket["id"])
    parts = [p for p in (subject, body) if p]
    return "\n\n".join(parts).strip()


def fetch_tickets(
    client: httpx.Client,
    statuses: str | None,
    from_date: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Список тикетов с фильтром по статусу и дате создания (GET /tickets/)."""
    params: dict = {}
    if statuses:
        params["status_list"] = statuses
    if from_date:
        params["from_date_created"] = from_date
    return paginate(client, "/tickets/", params, limit=limit)


def get_ticket_text(ticket_id: str | int) -> dict:
    """Прочитать тему и текст обращения по ID тикета (только GET).

    Возвращает {id, subject, text}. При ошибке text/subject — пустые строки.
    """
    try:
        with make_client() as client:
            try:
                resp = client.get(f"/tickets/{ticket_id}/")
                resp.raise_for_status()
                ticket = (resp.json() or {}).get("data") or {}
            except httpx.HTTPError as e:
                log.warning("не удалось прочитать тикет %s: %s", ticket_id, e)
                ticket = {}
            subject = ticket_subject(ticket) if isinstance(ticket, dict) else ""
            body = ticket_body(client, ticket_id)
    except (ValueError, httpx.HTTPError) as e:
        log.exception("get_ticket_text failed: ticket=%s", ticket_id)
        return {"id": str(ticket_id), "subject": "", "text": "", "error": str(e)}

    text = "\n\n".join(p for p in (subject, body) if p).strip()
    return {"id": str(ticket_id), "subject": subject, "text": text}


# --------------------------------------------------------------------------- #
# LangGraph-инструмент (async-обёртка над sync GET)
# --------------------------------------------------------------------------- #
@tool
async def get_task_details(task_id: str) -> dict:
    """Получить тему и текст задачи HelpDesk по ID (только чтение).

    Возвращает {id, subject, text}.
    """
    return await asyncio.to_thread(get_ticket_text, task_id)
