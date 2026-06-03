"""
RAG-пайплайн: тикет → поиск в базе знаний (Qdrant) → инструкция от DeepSeek.

Использование:
    from rag import RagPipeline
    answer = RagPipeline().answer("ПУ Меркурий не выходит на связь")
    print(answer.instruction)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .llm import DeepSeekClient
from .prompts import NO_CONTEXT, RAG_SYSTEM, USER_TEMPLATE
from .vector_store import VectorStore

log = logging.getLogger(__name__)


@dataclass
class RagAnswer:
    instruction: str
    sources: list[dict]
    model: str | None = None


class RagPipeline:
    def __init__(
        self,
        store: VectorStore | None = None,
        llm: DeepSeekClient | None = None,
    ) -> None:
        self.store = store or VectorStore()
        self.llm = llm or DeepSeekClient()

    @staticmethod
    def _format_context(hits: list[dict]) -> str:
        blocks = []
        for i, h in enumerate(hits, 1):
            blocks.append(
                f"[{i}] Статья: {h['title']} (файл: {h['source']})\n{h['text']}"
            )
        return "\n\n---\n\n".join(blocks)

    @staticmethod
    def _sources(hits: list[dict]) -> list[dict]:
        return [
            {
                "title": h["title"],
                "source": h["source"],
                "category": h.get("category"),
                "score": round(float(h["score"]), 3),
            }
            for h in hits
        ]

    def answer(self, ticket: str, top_k: int | None = None) -> RagAnswer:
        ticket = (ticket or "").strip()
        if not ticket:
            raise ValueError("Пустой текст тикета")

        hits = self.store.search(ticket, top_k=top_k)
        if not hits:
            log.info("RAG: релевантных статей не найдено")
            return RagAnswer(instruction=NO_CONTEXT, sources=[])

        messages = [
            {"role": "system", "content": RAG_SYSTEM},
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    ticket=ticket, context=self._format_context(hits)
                ),
            },
        ]
        out = self.llm.chat(messages)
        return RagAnswer(
            instruction=out["text"],
            sources=self._sources(hits),
            model=out.get("model"),
        )
