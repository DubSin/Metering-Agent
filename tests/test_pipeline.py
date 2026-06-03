"""Тесты RAG-пайплайна на заглушках (без Qdrant/fastembed/сети)."""
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


def test_answer_no_hits_skips_llm():
    store = FakeStore([])
    llm = FakeLLM()
    pipe = RagPipeline(store=store, llm=llm)

    ans = pipe.answer("вопрос без ответа в базе")

    assert ans.instruction == NO_CONTEXT
    assert ans.sources == []
    assert llm.messages is None  # LLM не вызывался


@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_answer_rejects_empty_ticket(bad):
    pipe = RagPipeline(store=FakeStore(HITS), llm=FakeLLM())
    with pytest.raises(ValueError):
        pipe.answer(bad)
