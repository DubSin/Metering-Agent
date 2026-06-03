#!/usr/bin/env python3
"""
Задать RAG тикет/вопрос и получить инструкцию по решению.

Запуск:
    python -m rag.ask "Меркурий 206 не выходит на связь, что делать?"
    echo "текст тикета" | python -m rag.ask
    python -m rag.ask --top-k 8 --json "текст тикета"
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

from rag.pipeline import RagPipeline  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ticket", nargs="*", help="Текст тикета (или подайте через stdin)")
    ap.add_argument("--top-k", type=int, default=None, help="Сколько фрагментов искать")
    ap.add_argument("--json", action="store_true", help="Вывести результат как JSON")
    args = ap.parse_args()

    ticket = " ".join(args.ticket).strip()
    if not ticket and not sys.stdin.isatty():
        ticket = sys.stdin.read().strip()
    if not ticket:
        print("Не передан текст тикета.", file=sys.stderr)
        return 1

    answer = RagPipeline().answer(ticket, top_k=args.top_k)

    if args.json:
        print(
            json.dumps(
                {
                    "instruction": answer.instruction,
                    "sources": answer.sources,
                    "model": answer.model,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(answer.instruction)
    if answer.sources:
        print("\n— Источники (по релевантности):")
        for s in answer.sources:
            print(f"  • {s['title']}  [{s['source']}]  score={s['score']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
