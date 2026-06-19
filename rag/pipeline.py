"""
RAG-пайплайн: тикет → поиск в базе знаний (Qdrant) → инструкция от DeepSeek.

Использование:
    from rag import RagPipeline
    answer = RagPipeline().answer("ПУ Меркурий не выходит на связь")
    print(answer.instruction)
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from .directory import Directory, get_directory
from .llm import DeepSeekClient, make_llm
from .prompts import NO_CONTEXT, RAG_SYSTEM, USER_TEMPLATE
from .vector_store import VectorStore

log = logging.getLogger(__name__)


def _extract_instruction_field(text: str) -> str:
    """Достать значение "instruction" из битого/обрезанного JSON-ответа модели.

    Запасной путь, когда json.loads не справился (ответ обрезан по лимиту токенов
    и без закрывающей кавычки/скобки, либо внутри кривые экранирования). Берём всё
    после "instruction": " до закрывающей кавычки (учитывая экранированные кавычки
    внутри), затем раскрываем JSON-экранирования. Пусто — если поля нет.
    """
    m = re.search(r'"instruction"\s*:\s*"((?:\\.|[^"\\])*)', text, re.DOTALL)
    if not m:
        return ""
    raw = m.group(1)
    try:
        return json.loads(f'"{raw}"', strict=False).strip()
    except json.JSONDecodeError:
        # Раскрыть экранирования не вышло (оборвалось на полпути) — отдаём как есть.
        return raw.strip()


@dataclass
class RagAnswer:
    instruction: str
    sources: list[dict]
    model: str | None = None
    # Нашлось ли в базе знаний готовое решение по обращению.
    solution_found: bool = True
    # Термины из обращения, не раскрытые в базе знаний (модель их не поняла).
    unknown_terms: list[str] = field(default_factory=list)


class RagPipeline:
    def __init__(
        self,
        store: VectorStore | None = None,
        llm: DeepSeekClient | None = None,
        directory: Directory | None = None,
    ) -> None:
        self.store = store or VectorStore()
        self.llm = llm or make_llm()
        # Справочник клиентов/owner: расшифровка кодов и сокращений из тикета.
        self.directory = directory if directory is not None else get_directory()

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

        t_start = time.perf_counter()
        hits = self.store.search(ticket, top_k=top_k)
        t_search = time.perf_counter() - t_start
        # Даже без релевантных статей не отбиваемся «решения нет»: прогоняем через
        # модель с пустым контекстом — она выдаёт предполагаемую инструкцию по
        # общим принципам АИИС КУЭ. Прямой инструкции в базе нет → solution_found=False.
        if not hits:
            log.info("RAG: релевантных статей не найдено — предполагаемая инструкция")
            context = NO_CONTEXT
        else:
            context = self._format_context(hits)

        # Справочник клиентов: точные совпадения owner/название/домен в тексте
        # тикета — отдаём LLM отдельным блоком (помогает раскрыть unknown_terms).
        directory_block = self.directory.format_block(self.directory.match(ticket))
        directory_section = f"{directory_block}\n" if directory_block else ""

        messages = [
            {"role": "system", "content": RAG_SYSTEM},
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    ticket=ticket, directory=directory_section, context=context
                ),
            },
        ]
        t_llm_start = time.perf_counter()
        out = self.llm.chat(messages)
        t_llm = time.perf_counter() - t_llm_start
        instruction, solution_found, unknown_terms = self._parse_output(out["text"])
        # Без статей готового решения в базе быть не может — фиксируем флаг жёстко.
        if not hits:
            solution_found = False
        log.info(
            "RAG: поиск %.2fc (статей=%d), LLM %.2fc (%s), всего %.2fc",
            t_search,
            len(hits),
            t_llm,
            out.get("model"),
            time.perf_counter() - t_start,
        )
        return RagAnswer(
            instruction=instruction,
            sources=self._sources(hits),
            model=out.get("model"),
            solution_found=solution_found,
            unknown_terms=unknown_terms,
        )

    @staticmethod
    def _parse_output(text: str) -> tuple[str, bool, list[str]]:
        """Разобрать JSON-ответ модели → (instruction, solution_found, unknown_terms).

        Модель просят вернуть строгий JSON, но на практике она может обернуть его в
        ```json … ``` или добавить текст вокруг. Достаём первый {…}-блок и парсим.
        Если разобрать не удалось — отдаём текст как есть (graceful degradation):
        считаем, что решение есть, без списка непонятых терминов.
        """
        text = (text or "").strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                # strict=False — модель часто кладёт «сырые» переводы строк прямо
                # внутрь строкового значения instruction; строгий парсер на таких
                # управляющих символах падает, и весь JSON улетал в Telegram сырым.
                data = json.loads(match.group(0), strict=False)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and "instruction" in data:
                instruction = str(data.get("instruction") or "").strip()
                solution_found = bool(data.get("solution_found", True))
                raw_terms = data.get("unknown_terms") or []
                unknown_terms = [
                    str(t).strip()
                    for t in raw_terms
                    if isinstance(raw_terms, list) and str(t).strip()
                ]
                return instruction or text, solution_found, unknown_terms
        # JSON не разобрался (обрезан по лимиту токенов / битые кавычки). Не вываливаем
        # сырой JSON в чат — пытаемся выдрать поле instruction регуляркой.
        instruction = _extract_instruction_field(text)
        if instruction:
            log.warning("RAG: JSON не разобран, instruction извлечён регуляркой")
            return instruction, True, []
        log.warning("RAG: ответ модели не разобран как JSON, отдаю как есть")
        return text, True, []
