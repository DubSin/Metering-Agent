#!/usr/bin/env python3
"""
Экспорт решений операторов из SQLite в JSONL для дообучения модели.

Берёт ревью со статусами:
  • approve — эталонным ответом считается инструкция ИИ (ai_instruction);
  • edited  — эталоном считается собственный ответ оператора (operator_answer).
Каждая строка — пример в chat-формате файнтюна:
  {"messages": [{"role":"system",...},{"role":"user",...},{"role":"assistant",...}]}

Опционально (--declines) отдельным файлом выгружаются отклонённые примеры —
полезно как negatives при анализе качества.

Запуск:
    python -m scripts.export_dataset --out dataset.jsonl
    python -m scripts.export_dataset --out dataset.jsonl --declines declines.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from rag.prompts import RAG_SYSTEM  # noqa: E402
from tgbot import store  # noqa: E402


def _example(ticket_text: str, assistant: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": RAG_SYSTEM},
            {"role": "user", "content": ticket_text},
            {"role": "assistant", "content": assistant},
        ]
    }


def _write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="dataset.jsonl", help="JSONL для approve+edited")
    ap.add_argument(
        "--declines",
        help="Отдельный JSONL для отклонённых примеров (negatives), опционально",
    )
    args = ap.parse_args()

    positives: list[dict] = []
    for r in store.iter_reviews([store.STATUS_APPROVE, store.STATUS_EDITED]):
        if r["status"] == store.STATUS_EDITED:
            assistant = (r.get("operator_answer") or "").strip()
        else:
            assistant = (r.get("ai_instruction") or "").strip()
        ticket_text = (r.get("ticket_text") or "").strip()
        if not assistant or not ticket_text:
            continue
        positives.append(_example(ticket_text, assistant))

    _write_jsonl(args.out, positives)
    print(f"Записано {len(positives)} примеров в {args.out}")

    if args.declines:
        declines: list[dict] = []
        for r in store.iter_reviews([store.STATUS_DECLINE]):
            ticket_text = (r.get("ticket_text") or "").strip()
            rejected = (r.get("ai_instruction") or "").strip()
            if not ticket_text:
                continue
            declines.append(
                {
                    "ticket_id": r["ticket_id"],
                    "ticket_text": ticket_text,
                    "rejected_instruction": rejected,
                }
            )
        _write_jsonl(args.declines, declines)
        print(f"Записано {len(declines)} отклонённых в {args.declines}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
