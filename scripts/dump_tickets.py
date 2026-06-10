#!/usr/bin/env python3
"""
Выгрузить тикеты из HelpDeskEddy (API v2) на локальный диск — ТОЛЬКО GET-запросы.

Зачем: один раз скачиваем образцы тикетов, дальше гоняем по ним RAG офлайн
(см. scripts/process_tickets.py --from-dir), не дёргая HelpDesk на каждой итерации.

Для каждого тикета сохраняется ticket_<id>.json:
    {
      "id", "subject", "body", "text",      # text = subject + body, готов для RAG
      "raw_ticket": {...},                   # сырой объект тикета из списка/детали
      "posts": [...]                         # сырые посты (GET /tickets/{id}/posts/)
    }
Плюс index.json со сводкой по всем выгруженным тикетам.

Запуск:
    python -m scripts.dump_tickets --status open --limit 20
    python -m scripts.dump_tickets --status open --from-date "2026-06-01 00:00:00"
    python -m scripts.dump_tickets --ticket 12345 --ticket 12346
    python -m scripts.dump_tickets --status open --limit 50 --out-dir ./ticket_samples
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Переиспользуем GET-only клиент и парсеры из общего модуля (без RAG-логики).
from tools.helpdesk_tools import (  # noqa: E402
    api_base,
    fetch_tickets,
    make_client,
    paginate,
    ticket_subject,
    ticket_text,
)


def ticket_detail(client: httpx.Client, ticket_id: str | int) -> dict:
    """GET /tickets/{id}/ — полный объект тикета (если доступен)."""
    try:
        resp = client.get(f"/tickets/{ticket_id}/")
        resp.raise_for_status()
        data = (resp.json() or {}).get("data")
        if isinstance(data, dict):
            # иногда data = {id: obj}, иногда сам obj
            if str(ticket_id) in data and isinstance(data[str(ticket_id)], dict):
                return data[str(ticket_id)]
            return data
    except httpx.HTTPError as e:
        print(f"  ! деталь тикета {ticket_id} недоступна: {e}", file=sys.stderr)
    return {"id": ticket_id}


def raw_posts(client: httpx.Client, ticket_id: str | int) -> list[dict]:
    try:
        return paginate(client, f"/tickets/{ticket_id}/posts/", {})
    except httpx.HTTPError as e:
        print(f"  ! посты тикета {ticket_id} недоступны: {e}", file=sys.stderr)
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ticket",
        action="append",
        dest="tickets",
        help="ID тикета для выгрузки (можно указать несколько раз)",
    )
    ap.add_argument("--status", default="open", help="status_list для выборки (по умолчанию open)")
    ap.add_argument("--from-date", help='Тикеты не раньше даты "YYYY-MM-DD HH:MM:SS"')
    ap.add_argument("--limit", type=int, default=20, help="Максимум тикетов (по умолчанию 20)")
    ap.add_argument(
        "--out-dir",
        default="./ticket_samples",
        help="Куда сохранять (по умолчанию ./ticket_samples)",
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []

    with make_client() as client:
        print(f"API: {api_base()}")

        if args.tickets:
            tickets = [ticket_detail(client, tid) for tid in args.tickets]
        else:
            tickets = fetch_tickets(client, args.status, args.from_date, args.limit)
        print(f"Тикетов к выгрузке: {len(tickets)}\n")

        for i, ticket in enumerate(tickets, 1):
            tid = ticket.get("id")
            if tid is None:
                continue
            posts = raw_posts(client, tid)
            text = ticket_text(client, ticket)
            record = {
                "id": tid,
                "subject": ticket_subject(ticket),
                "text": text,
                "raw_ticket": ticket,
                "posts": posts,
            }
            (out_dir / f"ticket_{tid}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            chars = len(text)
            print(f"[{i}/{len(tickets)}] тикет {tid}: {record['subject'] or '—'} ({chars} симв.)")
            summary.append({"id": tid, "subject": record["subject"], "chars": chars})

    (out_dir / "index.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nГотово. Файлы в {out_dir.resolve()} (index.json — сводка).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
