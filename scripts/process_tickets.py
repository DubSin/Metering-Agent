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
from tools.helpdesk_tools import (  # noqa: E402
    api_base,
    fetch_tickets,
    make_client,
    ticket_subject,
    ticket_text,
)


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
