"""Тесты общей обработки тикета (push/pull) — без RAG/сети."""
import pytest

import processing
from config import config
from tgbot import store


class FakeAnswer:
    instruction = "инструкция"
    sources = [{"title": "T", "source": "s"}]
    model = "deepseek"
    solution_found = True
    unknown_terms = []


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


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire_poller(monkeypatch, tmp_path, tickets):
    """Замокать сеть HelpDesk для process_new_tickets. Возвращает (pipe, sent, calls).

    fetch_tickets отдаёт переданные тикеты, отфильтрованные по from_date
    (включающий фильтр — эмуляция реального API). calls фиксирует from_date.
    """
    pipe, sent = _wire(monkeypatch, tmp_path)
    calls = {}

    # send_for_review мокаем так, чтобы он реально писал pending — иначе
    # count_today() не растёт и daily_index не увеличивается между тикетами.
    def _fake_send(**kw):
        store.create_pending(
            ticket_id=kw["ticket_id"],
            chat_id=kw.get("chat_id") or "-100",
            message_id=1,
            ticket_text=kw.get("ticket_text") or "",
            ai_instruction=kw.get("instruction") or "",
        )
        sent.append(kw)
        return 111

    def _fake_fetch(client, statuses, from_date=None, limit=None):
        calls["from_date"] = from_date
        if from_date is None:
            return list(tickets)
        return [t for t in tickets if t["date_created"] >= from_date]  # включающий

    monkeypatch.setattr(processing, "send_for_review", _fake_send)
    monkeypatch.setattr(processing, "make_client", lambda: _FakeClient())
    monkeypatch.setattr(processing, "fetch_tickets", _fake_fetch)
    monkeypatch.setattr(processing, "ticket_text", lambda client, t: f"текст {t['id']}")
    monkeypatch.setattr(processing, "ticket_subject", lambda t: f"тема {t['id']}")
    return pipe, sent, calls


def test_process_new_tickets_first_run_inits_watermark(tmp_path, monkeypatch):
    backlog = [
        {"id": 10, "date_created": "2026-06-16 10:00:00"},
        {"id": 12, "date_created": "2026-06-16 12:00:00"},
        {"id": 11, "date_created": "2026-06-16 11:00:00"},
    ]
    _, sent, _ = _wire_poller(monkeypatch, tmp_path, backlog)
    summary = processing.process_new_tickets()
    assert summary["initialized"] is True
    assert summary["sent"] == []               # бэклог не рассылаем
    assert sent == []
    # знак строго позже самого свежего из бэклога (12:00:00) → бэклог не зацепится
    assert summary["watermark"] > "2026-06-16 12:00:00"
    assert store.get_meta(store.META_POLLER_LAST_DATE) == summary["watermark"]


def test_process_new_tickets_sends_only_new(tmp_path, monkeypatch):
    tickets = [
        {"id": 12, "date_created": "2026-06-16 12:00:00"},  # на знаке, уже обработан ниже
        {"id": 13, "date_created": "2026-06-16 13:00:00"},
        {"id": 14, "date_created": "2026-06-16 14:00:00"},
    ]
    _, sent, calls = _wire_poller(monkeypatch, tmp_path, tickets)
    store.set_meta(store.META_POLLER_LAST_DATE, "2026-06-16 12:00:00")
    store.create_pending("12", "-100", 1, "txt", "instr")  # 12 уже на ревью

    summary = processing.process_new_tickets()
    assert calls["from_date"] == "2026-06-16 12:00:00"   # запрос от знака (включающий)
    assert summary["skipped"] == ["12"]                  # дубль на границе отсеян
    assert summary["sent"] == ["13", "14"]               # отправлены только новые
    assert summary["watermark"] == "2026-06-16 14:00:00"
    assert store.get_meta(store.META_POLLER_LAST_DATE) == "2026-06-16 14:00:00"
    # счётчик за сегодня идёт по порядку; #12 уже создан сегодня в этом тесте,
    # поэтому новые — 2-й и 3-й за сегодня.
    assert [s["daily_index"] for s in sent] == [2, 3]


def test_process_new_tickets_same_second_sibling_caught(tmp_path, monkeypatch):
    # два тикета в одну секунду: знак на этой секунде, один уже обработан,
    # второй (брат-близнец) должен подхватиться благодаря включающему фильтру.
    tickets = [
        {"id": 30, "date_created": "2026-06-16 15:53:09"},
        {"id": 31, "date_created": "2026-06-16 15:53:09"},
    ]
    _, sent, _ = _wire_poller(monkeypatch, tmp_path, tickets)
    store.set_meta(store.META_POLLER_LAST_DATE, "2026-06-16 15:53:09")
    store.create_pending("30", "-100", 1, "txt", "instr")  # 30 уже обработан

    summary = processing.process_new_tickets()
    assert summary["skipped"] == ["30"]
    assert summary["sent"] == ["31"]                     # близнец не потерян
    assert summary["watermark"] == "2026-06-16 15:53:09"


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
