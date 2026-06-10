"""Тесты общей обработки тикета (push/pull) — без RAG/сети."""
import pytest

import processing
from config import config
from tgbot import store


class FakeAnswer:
    instruction = "инструкция"
    sources = [{"title": "T", "source": "s"}]
    model = "deepseek"


class FakePipeline:
    def __init__(self):
        self.calls = []

    def answer(self, text, top_k=None):
        self.calls.append(text)
        return FakeAnswer()


def _wire(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "feedback_db", str(tmp_path / "f.sqlite3"))
    pipe = FakePipeline()
    sent = []
    monkeypatch.setattr(processing, "get_pipeline", lambda: pipe)
    monkeypatch.setattr(
        processing, "send_for_review", lambda **kw: sent.append(kw) or 111
    )
    return pipe, sent


def test_process_ticket_sends(tmp_path, monkeypatch):
    pipe, sent = _wire(monkeypatch, tmp_path)
    ok = processing.process_ticket("110773", text="Меркурий не на связи")
    assert ok is True
    assert pipe.calls == ["Меркурий не на связи"]
    assert sent[0]["ticket_id"] == "110773"
    assert sent[0]["instruction"] == "инструкция"


def test_process_ticket_skip_existing(tmp_path, monkeypatch):
    pipe, sent = _wire(monkeypatch, tmp_path)
    store.create_pending("110773", "-100", 1, "txt", "instr")
    ok = processing.process_ticket("110773", text="что-то", skip_existing=True)
    assert ok is False
    assert pipe.calls == []      # RAG не дёргался
    assert sent == []            # рассылки не было


def test_process_ticket_empty_text(tmp_path, monkeypatch):
    pipe, sent = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(
        processing, "get_ticket_text", lambda tid: {"id": tid, "subject": "", "text": ""}
    )
    ok = processing.process_ticket("999")  # текста нет нигде
    assert ok is False
    assert sent == []


def test_fetch_and_review_by_ids(tmp_path, monkeypatch):
    pipe, sent = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(
        processing, "get_ticket_text",
        lambda tid: {"id": tid, "subject": "тема", "text": f"текст {tid}"},
    )
    store.create_pending("111", "-100", 1, "txt", "instr")  # уже на ревью

    summary = processing.fetch_and_review(ids=["111", "222"])
    assert summary["skipped"] == ["111"]      # дедуп
    assert summary["sent"] == ["222"]         # новый отправлен
    assert summary["found"] == 2


def test_format_fetch_summary():
    from tgbot.commands import format_fetch_summary

    out = format_fetch_summary({"found": 3, "sent": ["1", "2"], "skipped": ["3"], "empty": []})
    assert "найдено: 3" in out
    assert "#1" in out and "#2" in out
    assert "Уже на ревью" in out
    empty = format_fetch_summary({"found": 0, "sent": [], "skipped": [], "empty": []})
    assert "Новых тикетов нет" in empty


def test_llm_provider_switch(monkeypatch):
    monkeypatch.setattr(processing, "_pipeline", None)
    monkeypatch.setattr(processing, "_provider_override", None)

    assert processing.current_provider() == config.llm_provider  # дефолт из config

    processing.set_provider("ollama")
    assert processing.current_provider() == "ollama"

    processing.set_provider("deepseek")
    assert processing.current_provider() == "deepseek"

    with pytest.raises(ValueError):
        processing.set_provider("gpt5")  # неизвестный провайдер
