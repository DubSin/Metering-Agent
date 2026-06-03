"""
Хранилище векторов на Qdrant: создание коллекции, индексация чанков базы знаний
и поиск релевантных фрагментов под запрос/тикет.

QDRANT_URL поддерживает три формы:
  http(s)://host:port — внешний сервер Qdrant;
  ":memory:"          — встроенный инстанс в памяти (для тестов);
  путь на диске       — встроенное локальное хранилище.
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from config import config

from .embeddings import Embedder
from .kb_loader import Chunk

if TYPE_CHECKING:  # только для типов — без рантайм-зависимости от qdrant_client
    from qdrant_client import QdrantClient

log = logging.getLogger(__name__)


def make_client() -> "QdrantClient":
    from qdrant_client import QdrantClient

    url = config.rag.qdrant_url.strip()
    if url in (":memory:", "memory"):
        return QdrantClient(location=":memory:")
    if "://" in url:
        return QdrantClient(url=url, api_key=config.rag.qdrant_api_key or None)
    return QdrantClient(path=url)


class VectorStore:
    def __init__(
        self,
        embedder: Embedder | None = None,
        client: "QdrantClient | None" = None,
    ) -> None:
        self.embedder = embedder or Embedder()
        self.client = client or make_client()
        self.collection = config.rag.collection

    def ensure_collection(self, recreate: bool = False) -> None:
        from qdrant_client import models

        exists = self.client.collection_exists(self.collection)
        if exists and recreate:
            self.client.delete_collection(self.collection)
            exists = False
        if not exists:
            self.client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(
                    size=self.embedder.dim,
                    distance=models.Distance.COSINE,
                ),
            )

    def index(self, chunks: list[Chunk], batch_size: int = 64) -> int:
        """Векторизовать и загрузить чанки. id детерминирован по uid (идемпотентно)."""
        from qdrant_client import models

        total = 0
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            vectors = self.embedder.embed_passages([c.text for c in batch])
            points = [
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, c.uid)),
                    vector=vector,
                    payload={
                        "text": c.text,
                        "title": c.title,
                        "source": c.source,
                        "category": c.category,
                        "chunk_index": c.chunk_index,
                        "meta": c.meta,
                    },
                )
                for c, vector in zip(batch, vectors)
            ]
            self.client.upsert(self.collection, points=points)
            total += len(points)
            log.info("проиндексировано %d/%d", total, len(chunks))
        return total

    def search(self, query: str, top_k: int | None = None) -> list[dict]:
        vector = self.embedder.embed_query(query)
        res = self.client.query_points(
            self.collection,
            query=vector,
            limit=top_k or config.rag.top_k,
            with_payload=True,
        )
        return [{**point.payload, "score": point.score} for point in res.points]
