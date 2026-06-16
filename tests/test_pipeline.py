"""Тесты RAG-пайплайна на заглушках (без Qdrant/fastembed/сети)."""
import json

import pytest

from rag.pipeline import RagPipeline
from rag.prompts import NO_CONTEXT


class FakeStore:
    def __init__(self, hits):
        self._hits = hits
        self.last_query = None
        self.last_top_k = None

    def search(self, query, top_k=None):
        self.last_query = query
        self.last_top_k = top_k
        return self._hits


class FakeLLM:
    def __init__(self, text="ответ"):
        self.text = text
        self.messages = None

    def chat(self, messages, **kwargs):
        self.messages = messages
        return {"text": self.text, "model": "deepseek-chat", "raw": {}}


HITS = [
    {
        "text": "Штатные пароли\n\nдля меркуриев 111111",
        "title": "Штатные пароли",
        "source": "ПНР/100-Штатные пароли.html",
        "category": "ПНР",
        "chunk_index": 0,
        "score": 0.876543,
    },
    {
        "text": "Реджойн\n\nкоманда 0FFF на порт 223",
        "title": "Если ПУ залип в реджойне",
        "source": "ПНР/43-Если ПУ залип в реджойне.html",
        "category": "ПНР",
        "chunk_index": 1,
        "score": 0.7,
    },
]


def test_answer_builds_context_and_returns_sources():
    store = FakeStore(HITS)
    llm = FakeLLM(text="готовая инструкция")
    pipe = RagPipeline(store=store, llm=llm)

    ans = pipe.answer("Меркурий не выходит на связь", top_k=4)

    assert ans.instruction == "готовая инструкция"
    assert ans.model == "deepseek-chat"
    assert store.last_query == "Меркурий не выходит на связь"
    assert store.last_top_k == 4

    # в контекст пользовательского сообщения попали оба фрагмента
    user_msg = llm.messages[1]["content"]
    assert "Штатные пароли" in user_msg
    assert "команда 0FFF на порт 223" in user_msg
    assert "Меркурий не выходит на связь" in user_msg

    # источники: нужные поля и округлённый score
    assert ans.sources[0] == {
        "title": "Штатные пароли",
        "source": "ПНР/100-Штатные пароли.html",
        "category": "ПНР",
        "score": 0.877,
    }
    assert len(ans.sources) == 2


def test_answer_no_hits_still_calls_llm_for_presumed_instruction():
    store = FakeStore([])
    payload = json.dumps(
        {"solution_found": True, "instruction": "1. Суть\n2. Шаги", "unknown_terms": []},
        ensure_ascii=False,
    )
    llm = FakeLLM(text=payload)
    pipe = RagPipeline(store=store, llm=llm)

    ans = pipe.answer("вопрос без ответа в базе")

    # LLM вызван и выдал предполагаемую инструкцию...
    assert llm.messages is not None
    assert ans.instruction == "1. Суть\n2. Шаги"
    assert ans.sources == []
    # ...но без статей в базе прямого решения быть не может — флаг принудительно False.
    assert ans.solution_found is False
    # В контекст модели подставлена заглушка NO_CONTEXT.
    assert NO_CONTEXT in llm.messages[1]["content"]


def test_answer_parses_json_solution_and_terms():
    store = FakeStore(HITS)
    payload = json.dumps(
        {
            "solution_found": True,
            "instruction": "1. Суть\n2. Шаги",
            "unknown_terms": ["УСПД", "  "],
        },
        ensure_ascii=False,
    )
    pipe = RagPipeline(store=store, llm=FakeLLM(text=payload))

    ans = pipe.answer("Меркурий не выходит на связь")

    assert ans.instruction == "1. Суть\n2. Шаги"
    assert ans.solution_found is True
    assert ans.unknown_terms == ["УСПД"]  # пустые термины отброшены


def test_answer_json_no_solution_found():
    store = FakeStore(HITS)
    payload = "```json\n" + json.dumps(
        {"solution_found": False, "instruction": "нет решения", "unknown_terms": []},
        ensure_ascii=False,
    ) + "\n```"
    pipe = RagPipeline(store=store, llm=FakeLLM(text=payload))

    ans = pipe.answer("экзотический вопрос")

    assert ans.solution_found is False
    assert ans.instruction == "нет решения"


def test_answer_non_json_falls_back_to_plain_text():
    store = FakeStore(HITS)
    pipe = RagPipeline(store=store, llm=FakeLLM(text="просто текст без json"))

    ans = pipe.answer("Меркурий не выходит на связь")

    assert ans.instruction == "просто текст без json"
    assert ans.solution_found is True
    assert ans.unknown_terms == []


@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_answer_rejects_empty_ticket(bad):
    pipe = RagPipeline(store=FakeStore(HITS), llm=FakeLLM())
    with pytest.raises(ValueError):
        pipe.answer(bad)
