#!/usr/bin/env python3
"""
Вытянуть тикеты из HelpDeskEddy (API v2) и прогнать каждый через RAG,
получив инструкцию по решению.

Что делает:
  1. Читает список тикетов (GET /tickets/) постранично, с фильтром по статусу
     и дате создания.
  2. Для каждого тикета берёт тело первого поста (GET /tickets/{id}/posts/),
     т.к. сам /tickets/{id} текст обращения не возвращает.
  3. Склеивает тему + текст и отдаёт в RagPipeline → инструкция + источники.
  4. Печатает результат и (опц.) сохраняет всё в JSON.

Аутентификация: Basic (email:api_key) — параметры из config / .env:
    HELPDESK_EDDY_BASE_URL, HELPDESK_EDDY_EMAIL, HELPDESK_EDDY_API_KEY

Запуск:
    python -m scripts.process_tickets --status open --limit 20
    python -m scripts.process_tickets --status open --from-date "2026-06-01 00:00:00"
    python -m scripts.process_tickets --ticket 12345 --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

# Позволяет запускать как `python scripts/process_tickets.py`, так и `-m scripts.process_tickets`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# На Windows консоль часто в cp1251 — принудительно пишем в UTF-8,
# иначе падает на кириллице и символах рамок.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv не обязателен
    pass

from config import config  # noqa: E402
from rag.pipeline import RagPipeline  # noqa: E402


# --------------------------------------------------------------------------- #
# HelpDeskEddy API v2 клиент
# --------------------------------------------------------------------------- #
def api_base() -> str:
    """Сформировать базу API v2 из HELPDESK_EDDY_BASE_URL (только схема и хост)."""
    parts = urlsplit(config.helpdesk_eddy.base_url)
    if not parts.scheme or not parts.netloc:
        raise SystemExit(
            "HELPDESK_EDDY_BASE_URL задан некорректно: "
            f"{config.helpdesk_eddy.base_url!r}"
        )
    return f"{parts.scheme}://{parts.netloc}/api/v2"


def make_client() -> httpx.Client:
    email = config.helpdesk_eddy.email
    api_key = config.helpdesk_eddy.api_key
    if not email or not api_key:
        raise SystemExit(
            "Нужны HELPDESK_EDDY_EMAIL и HELPDESK_EDDY_API_KEY "
            "(Basic-аутентификация API v2). Заполни их в .env."
        )
    return httpx.Client(
        base_url=api_base(),
        auth=(email, api_key),  # Basic email:api_key
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
def _strip_html(html: str) -> str:
    """Убрать разметку из тела поста (тот же подход, что в rag.kb_loader)."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # грубый фолбэк без bs4
        import re

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
        print(f"  ! не удалось прочитать посты тикета {ticket_id}: {e}", file=sys.stderr)
        return ""
    if not posts:
        return ""
    # первый по времени пост — исходное обращение клиента
    posts.sort(key=lambda p: p.get("date_created") or p.get("id") or 0)
    first = posts[0]
    return _strip_html(_pick(first, "message", "text", "body", "content"))


def ticket_text(client: httpx.Client, ticket: dict) -> str:
    """Склеить тему и тело обращения в один текст для RAG."""
    subject = ticket_subject(ticket)
    body = ticket_body(client, ticket["id"])
    parts = [p for p in (subject, body) if p]
    return "\n\n".join(parts).strip()


def fetch_tickets(
    client: httpx.Client,
    statuses: str | None,
    from_date: str | None,
    limit: int | None,
) -> list[dict]:
    params: dict = {}
    if statuses:
        params["status_list"] = statuses
    if from_date:
        params["from_date_created"] = from_date
    return paginate(client, "/tickets/", params, limit=limit)


# --------------------------------------------------------------------------- #
# Офлайн-прогон по скачанным файлам (RAG без обращений к HelpDesk)
# --------------------------------------------------------------------------- #
def run_offline(pipeline: RagPipeline, args) -> int:
    src = Path(args.from_dir)
    files = sorted(src.glob("ticket_*.json"))
    if not files:
        print(f"В {src} нет файлов ticket_*.json", file=sys.stderr)
        return 1

    results: list[dict] = []
    print(f"Офлайн-прогон из {src.resolve()} ({len(files)} тикетов)\n")

    for i, fp in enumerate(files, 1):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        tid = rec.get("id")
        text = (rec.get("text") or "").strip()
        if not text:
            print(f"[{i}/{len(files)}] тикет {tid}: пустой текст, пропуск\n")
            results.append({"ticket_id": tid, "skipped": "empty"})
            continue

        answer = pipeline.answer(text, top_k=args.top_k)
        print(f"[{i}/{len(files)}] тикет {tid}: {rec.get('subject') or '—'}")
        print("─" * 70)
        print(answer.instruction)
        if answer.sources:
            print("\n— Источники:")
            for s in answer.sources:
                print(f"  • {s['title']}  [{s['source']}]  score={s['score']}")
        print()

        results.append(
            {
                "ticket_id": tid,
                "subject": rec.get("subject"),
                "ticket_text": text,
                "instruction": answer.instruction,
                "sources": answer.sources,
                "model": answer.model,
            }
        )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Результат сохранён в {args.json_out}")
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ticket",
        help="Обработать один тикет по ID (вместо выборки списком)",
    )
    ap.add_argument(
        "--status",
        default="open",
        help="status_list для выборки тикетов (по умолчанию open)",
    )
    ap.add_argument(
        "--from-date",
        help='Тикеты не раньше даты, формат "YYYY-MM-DD HH:MM:SS"',
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Максимум тикетов для обработки (по умолчанию 20)",
    )
    ap.add_argument("--top-k", type=int, default=None, help="Сколько фрагментов искать в RAG")
    ap.add_argument("--json", dest="json_out", help="Сохранить результат в JSON-файл")
    ap.add_argument(
        "--from-dir",
        help="Офлайн: читать ticket_*.json из папки (см. scripts.dump_tickets), не дёргая HelpDesk",
    )
    args = ap.parse_args()

    pipeline = RagPipeline()
    results: list[dict] = []

    if args.from_dir:
        return run_offline(pipeline, args)

    with make_client() as client:
        print(f"API: {api_base()}")

        if args.ticket:
            tickets = [{"id": args.ticket}]
        else:
            tickets = fetch_tickets(client, args.status, args.from_date, args.limit)
        print(f"Тикетов к обработке: {len(tickets)}\n")

        for i, ticket in enumerate(tickets, 1):
            tid = ticket["id"]
            text = ticket_text(client, ticket)
            if not text:
                print(f"[{i}/{len(tickets)}] тикет {tid}: пустой текст, пропуск\n")
                results.append({"ticket_id": tid, "skipped": "empty"})
                continue

            answer = pipeline.answer(text, top_k=args.top_k)

            print(f"[{i}/{len(tickets)}] тикет {tid}: {ticket_subject(ticket) or '—'}")
            print("─" * 70)
            print(answer.instruction)
            if answer.sources:
                print("\n— Источники:")
                for s in answer.sources:
                    print(f"  • {s['title']}  [{s['source']}]  score={s['score']}")
            print()

            results.append(
                {
                    "ticket_id": tid,
                    "subject": ticket_subject(ticket),
                    "ticket_text": text,
                    "instruction": answer.instruction,
                    "sources": answer.sources,
                    "model": answer.model,
                }
            )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Результат сохранён в {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
