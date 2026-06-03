"""
Локальные эмбеддинги через fastembed.

Модель не требует обращения к DeepSeek-серверу (тот отдаёт только chat/completions),
поэтому векторизуем текст на месте. По умолчанию мультиязычная e5 — она хорошо
работает с русским техническим текстом базы знаний.

Для семейства e5 важны префиксы: «query:» для запроса и «passage:» для документа —
добавляем их автоматически.
"""
from __future__ import annotations

from functools import cached_property
from typing import Iterable

from config import config


class Embedder:
    def __init__(self, model_name: str | None = None) -> None:
        # fastembed импортируем лениво: тяжёлая зависимость, не нужна для
        # импорта пакета (юнит-тесты логики работают без неё).
        from fastembed import TextEmbedding

        self.model_name = model_name or config.rag.embed_model
        self._model = TextEmbedding(model_name=self.model_name)
        self._is_e5 = "e5" in self.model_name.lower()

    def _prep(self, texts: Iterable[str], kind: str) -> list[str]:
        texts = list(texts)
        if not self._is_e5:
            return texts
        prefix = "query: " if kind == "query" else "passage: "
        return [prefix + t for t in texts]

    def embed_passages(self, texts: Iterable[str]) -> list[list[float]]:
        prepared = self._prep(texts, "passage")
        return [v.tolist() for v in self._model.embed(prepared)]

    def embed_query(self, text: str) -> list[float]:
        prepared = self._prep([text], "query")
        return next(iter(self._model.embed(prepared))).tolist()

    @cached_property
    def dim(self) -> int:
        """Размерность вектора — определяем пробным эмбеддингом."""
        return len(self.embed_query("probe"))
