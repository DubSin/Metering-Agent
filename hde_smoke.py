"""Read-only смок-тест интеграции с HelpDeskEddy.

Дёргает только GET-эндпоинты — НИЧЕГО не пишет в тикет, не меняет статус,
не добавляет комментариев. Безопасно гонять на проде.

Запуск:
    python hde_smoke.py                       # список свежих open-тикетов (по фильтру из cfg)
    python hde_smoke.py 12345                 # + детали конкретного тикета
    python hde_smoke.py --since "2026-05-20 00:00:00"
    python hde_smoke.py --limit 10
    python hde_smoke.py --debug               # сырой ответ /tickets/ без фильтра + статусы + сводка
    python hde_smoke.py --status open,process # переопределить фильтр статусов
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()  # подхватываем .env из корня проекта, если есть
except ImportError:
    pass

import httpx  # noqa: E402

from config import config  # noqa: E402
from tools.helpdesk_tools import (  # noqa: E402
    API_PREFIX,
    _auth_header,
    _unwrap,
    get_task_details,
    list_new_tickets,
)


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def _short(obj: Any, n: int = 600) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


async def _raw_get(path: str, params: dict | None = None) -> tuple[int, Any]:
    """Прямой GET без фильтров из cfg — для диагностики."""
    cfg = config.helpdesk_eddy
    async with httpx.AsyncClient(
        base_url=cfg.base_url.rstrip("/") + API_PREFIX,
        headers={**_auth_header(), "Accept": "application/json"},
        timeout=cfg.request_timeout,
    ) as c:
        r = await c.get(path, params=params or {})
        try:
            body = r.json() if r.content else None
        except Exception:
            body = r.text
        return r.status_code, body


async def _debug_mode(limit: int) -> int:
    """Что делать, если list_new_tickets говорит «пусто», а в UI тикеты есть:
    1) список статусов портала (slug ↔ id ↔ title) — если эндпоинт доступен;
    2) тикеты без фильтра, чтобы увидеть реальные status_id из ответа;
    3) сводка количества по status_id.
    """
    def _normalize_list(b: Any) -> list:
        """HDE возвращает data либо list, либо dict {id: {...}} — приводим к list."""
        d = _unwrap(b) if isinstance(b, (list, dict)) else b
        if isinstance(d, dict):
            if "tickets" in d or "result" in d:
                d = d.get("tickets") or d.get("result") or []
            else:
                d = [v for v in d.values() if isinstance(v, dict)]
        return d if isinstance(d, list) else []

    def _ru(v: Any) -> Any:
        """slug/title в HDE — мультиязычный словарь, берём ru."""
        if isinstance(v, dict):
            return v.get("ru") or v.get("en") or next(iter(v.values()), "")
        return v

    _print_header("GET /statuses/  (узнаём, какие статусы вообще есть в HDE)")
    code, body = await _raw_get("/statuses/")
    print(f"  HTTP {code}")
    items = _normalize_list(body)
    if items:
        for s in items:
            print(
                f"   - id={str(s.get('id')):<12} "
                f"slug/title='{_ru(s.get('title') or s.get('name') or s.get('slug'))}'"
            )
    else:
        print(f"  ответ: {_short(body)}")

    _print_header(f"GET /tickets/  БЕЗ фильтра по статусам (page=1, limit={limit})")
    code, body = await _raw_get("/tickets/", {"page": 1, "order_by": "-date_created"})
    print(f"  HTTP {code}")
    tickets = _normalize_list(body)
    if not tickets:
        print(f"  пусто или неожиданный формат: {_short(body)}")
        return 1

    print(f"  всего на странице: {len(tickets)}")
    by_status: Counter = Counter()
    for it in tickets[:limit]:
        if not isinstance(it, dict):
            continue
        by_status[it.get("status_id")] += 1
        print(
            f"   - id={str(it.get('id')):<8} "
            f"status_id={str(it.get('status_id')):<6} "
            f"created={it.get('date_created')} "
            f"| {it.get('title') or it.get('subject') or ''}"
        )

    _print_header("сводка по status_id")
    for sid, n in by_status.most_common():
        print(f"   - {sid}: {n}")

    print(
        "\n[i] Текущий фильтр в config.helpdesk_eddy.poll_status_list = "
        f"{config.helpdesk_eddy.poll_status_list!r}.\n"
        "    Если в списке выше тикеты идут с другим status_id/slug — обнови\n"
        "    HELPDESK_EDDY_POLL_STATUSES (через запятую) и запусти без --debug."
    )
    return 0


async def main(
    task_id: str | None,
    since: str | None,
    limit: int,
    debug: bool,
    status_override: str | None,
) -> int:
    cfg = config.helpdesk_eddy

    _print_header("config")
    print(f"  base_url       : {cfg.base_url}")
    print(f"  email          : {cfg.email or '<не задан>'}")
    print(f"  api_key        : {'***' + cfg.api_key[-4:] if cfg.api_key else '<не задан>'}")
    print(f"  poll_statuses  : {cfg.poll_status_list}")
    print(f"  request_timeout: {cfg.request_timeout}s")
    if not cfg.email or not cfg.api_key:
        print("\n[!] HELPDESK_EDDY_EMAIL / HELPDESK_EDDY_API_KEY не заданы — выходим.")
        return 2

    if status_override is not None:
        # ВНИМАНИЕ: правим только in-memory объект cfg, файлы/окружение не трогаем.
        cfg.poll_status_list = status_override
        print(f"[i] фильтр статусов переопределён на: {status_override!r}")

    if debug:
        return await _debug_mode(limit)

    _print_header(f"list_new_tickets (limit={limit}, since={since!r}, statuses={cfg.poll_status_list!r})")
    payload: dict = {"limit": limit}
    if since:
        payload["since"] = since
    items = await list_new_tickets.ainvoke(payload)

    if isinstance(items, list) and items and isinstance(items[0], dict) and items[0].get("error"):
        print(f"[!] ошибка API: {items[0]['error']}")
        return 1

    if not items:
        print("  (пусто) — заявок по фильтру нет")
        print("\n[подсказка] если в UI тикеты есть, фильтр статусов может не совпадать.")
        print("            запусти: python hde_smoke.py --debug")
    else:
        print(f"  получено: {len(items)}")
        for it in items:
            print(
                f"   - id={str(it.get('task_id')):<8} "
                f"status={str(it.get('status_id')):<10} "
                f"created={it.get('created_at')} "
                f"| {it.get('subject') or ''}"
            )

    if not task_id and isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("task_id"):
                task_id = str(it["task_id"])
                print(f"\n[i] task_id не задан аргументом, берём первый из списка: {task_id}")
                break

    if not task_id:
        print("\n[i] нет тикета для get_task_details — пропускаем")
        return 0

    _print_header(f"get_task_details(task_id={task_id})")
    meta = await get_task_details.ainvoke({"task_id": task_id})
    if meta.get("error"):
        print(f"[!] ошибка API: {meta['error']}")
        return 1

    print(f"  task_id     : {meta.get('task_id')}")
    print(f"  unique_id   : {meta.get('unique_id')}")
    print(f"  subject     : {meta.get('subject')}")
    print(f"  author      : {meta.get('author')}")
    print(f"  created_at  : {meta.get('created_at')}")
    print(f"  status_id   : {meta.get('status_id')}")
    print(f"  priority_id : {meta.get('priority_id')}")
    print(f"  department  : {meta.get('department_id')}")
    print(f"  attachments : {len(meta.get('attachments') or [])} шт.")
    text = meta.get("text") or ""
    preview = text[:300].replace("\n", " ")
    print(f"  text[:300]  : {preview}{'…' if len(text) > 300 else ''}")

    print("\n[ok] read-only smoke завершён — записи в HDE не производилось.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="HelpDeskEddy read-only smoke test")
    p.add_argument("task_id", nargs="?", help="ID тикета для get_task_details (опц.)")
    p.add_argument("--since", help="Фильтр from_date_created, YYYY-MM-DD HH:MM:SS")
    p.add_argument("--limit", type=int, default=5, help="Сколько тикетов вернуть (по умолчанию 5)")
    p.add_argument("--debug", action="store_true",
                   help="Диагностика: GET /statuses/ + GET /tickets/ без фильтра + сводка")
    p.add_argument("--status", dest="status_override",
                   help="Переопределить фильтр статусов на эту сессию (например: open,process,new)")
    args = p.parse_args()

    sys.exit(asyncio.run(main(
        args.task_id, args.since, args.limit, args.debug, args.status_override,
    )))
