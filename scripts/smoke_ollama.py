#!/usr/bin/env python3
"""
Дымовой тест связки RAG → Ollama: один реальный запрос к модели.

Проверяет по шагам:
  1. Доступен ли эндпоинт Ollama (GET /v1/models).
  2. Есть ли хотя бы одна модель (и какая будет выбрана).
  3. Проходит ли реальный chat-запрос (POST /v1/chat/completions).

Использует ту же фабрику make_llm('ollama'), что и RAG, поэтому
тестирует именно ту связку, что пойдёт в прод.

Запуск:
    python -m scripts.smoke_ollama
    python -m scripts.smoke_ollama --model llama3.1 --prompt "2+2?"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# На Windows консоль часто в cp1251 — пишем в UTF-8.
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

import httpx  # noqa: E402

from config import config  # noqa: E402
from rag.llm import make_llm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--model",
        help="Имя модели Ollama (по умолчанию OLLAMA_MODEL / первая из /v1/models)",
    )
    ap.add_argument(
        "--prompt",
        default="Ответь одним словом: работает?",
        help="Текст пробного запроса",
    )
    args = ap.parse_args()

    client = make_llm("ollama")
    if args.model:
        client._model = args.model

    print(f"Эндпоинт: {client.base_url}")

    # 1) доступность + список моделей
    try:
        models = client.list_models()
    except httpx.HTTPError as e:
        print(f"✗ Ollama недоступна по {client.base_url}: {e}", file=sys.stderr)
        print("  Проверь, что Ollama запущена (`ollama serve`) и порт верный.", file=sys.stderr)
        return 2

    if not models:
        print("✗ Ollama отвечает, но моделей нет. Скачай: `ollama pull llama3.1`", file=sys.stderr)
        return 3
    print(f"✓ Модели в Ollama ({len(models)}): {', '.join(models)}")

    # 2) какая будет выбрана
    try:
        chosen = client.model
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 3
    print(f"✓ Выбрана модель: {chosen}")
    if not config.ollama.model and not args.model:
        print("  (OLLAMA_MODEL не задан — взята первая из списка; задай явно в .env)")

    # 3) реальный chat-запрос
    print(f"\nЗапрос: {args.prompt!r}")
    try:
        out = client.chat([{"role": "user", "content": args.prompt}])
    except httpx.HTTPError as e:
        print(f"✗ chat-запрос упал: {e}", file=sys.stderr)
        return 4

    answer = (out["text"] or "").strip()
    if not answer:
        print("✗ Модель вернула пустой ответ.", file=sys.stderr)
        return 5

    print(f"✓ Ответ модели ({out['model']}):\n{answer}")
    print("\n✓ Связка RAG → Ollama работает.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
