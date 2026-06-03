#!/usr/bin/env python3
"""
Построение индекса базы знаний в Qdrant.

Читает knowledge_base/*.html, режет на чанки, эмбеддит локальной моделью
(fastembed) и загружает в коллекцию Qdrant.

Запуск:
    python -m rag.index                 # обычная индексация (идемпотентно)
    python -m rag.index --recreate      # пересоздать коллекцию с нуля
    python -m rag.index --kb-dir ./knowledge_base
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from config import config  # noqa: E402
from rag.kb_loader import load_chunks  # noqa: E402
from rag.vector_store import VectorStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kb-dir", default=config.rag.kb_dir, help="Папка базы знаний")
    ap.add_argument(
        "--recreate", action="store_true", help="Пересоздать коллекцию Qdrant"
    )
    ap.add_argument("--batch-size", type=int, default=64)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("Загружаю базу знаний…")
    chunks = load_chunks(args.kb_dir)
    print(f"  чанков: {len(chunks)}")
    if not chunks:
        print("Нет данных для индексации.", file=sys.stderr)
        return 1

    store = VectorStore()
    print(
        f"Эмбеддинг-модель: {store.embedder.model_name} (dim={store.embedder.dim})"
    )
    store.ensure_collection(recreate=args.recreate)
    n = store.index(chunks, batch_size=args.batch_size)

    print(f"\nГотово. Загружено {n} чанков в коллекцию '{config.rag.collection}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
