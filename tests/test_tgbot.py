"""Тесты Telegram-ревью: SQLite-хранилище и формирование рассылки (без сети)."""
import pytest

import httpx
import pytest

from config import config
from tgbot import client as tg_client
from tgbot import commands, notify, store


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "feedback_db", str(tmp_path / "feedback.sqlite3"))
    return config.feedback_db


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_create_pending_and_get(temp_db):
    store.create_pending(
        ticket_id="42",
        chat_id="-100123",
        message_id=555,
        ticket_text="Меркурий не на связи",
        ai_instruction="Сделать реджойн",
        ai_sources=[{"title": "Реджойн", "source": "ПНР/43.html"}],
        ai_model="deepseek",
        subject="Нет связи",
    )
    row = store.get("42")
    assert row["status"] == store.STATUS_PENDING
    assert row["message_id"] == 555
    assert row["ticket_text"] == "Меркурий не на связи"
    assert "Реджойн" in row["ai_sources"]


def test_record_decision_approve(temp_db):
    store.create_pending("7", "-100", 1, "txt", "instr")
    assert store.record_decision("7", store.STATUS_APPROVE, 999, "@op")
    row = store.get("7")
    assert row["status"] == store.STATUS_APPROVE
    assert row["operator_name"] == "@op"
    assert row["decided_at"] is not None


def test_record_decision_missing_ticket(temp_db):
    assert store.record_decision("nope", store.STATUS_DECLINE) is False


def test_set_operator_answer(temp_db):
    store.create_pending("9", "-100", 1, "txt", "instr")
    assert store.set_operator_answer("9", "Мой ответ", 1, "@op")
    row = store.get("9")
    assert row["status"] == store.STATUS_EDITED
    assert row["operator_answer"] == "Мой ответ"


def test_exists(temp_db):
    assert store.exists("13") is False
    store.create_pending("13", "-100", 1, "txt", "instr")
    assert store.exists("13") is True


def test_answer_prompt_roundtrip(temp_db):
    store.link_answer_prompt(777, "9")
    assert store.resolve_answer_prompt(777) == "9"
    # one-shot: повторно не находится
    assert store.resolve_answer_prompt(777) is None


def test_ticket_messages_roundtrip(temp_db):
    store.add_ticket_message("110773", "-100", 11)
    store.add_ticket_message("110773", "555", 22)   # та же заявка в другом чате
    store.add_ticket_message("999", "-100", 33)
    msgs = store.get_ticket_messages("110773")
    assert {(m["chat_id"], m["message_id"]) for m in msgs} == {("-100", 11), ("555", 22)}
    store.clear_ticket_messages("110773")
    assert store.get_ticket_messages("110773") == []
    assert len(store.get_ticket_messages("999")) == 1  # чужие не тронуты


def test_iter_reviews_filter(temp_db):
    store.create_pending("1", "-100", 1, "t1", "i1")
    store.create_pending("2", "-100", 2, "t2", "i2")
    store.record_decision("1", store.STATUS_APPROVE)
    approved = store.iter_reviews([store.STATUS_APPROVE])
    assert [r["ticket_id"] for r in approved] == ["1"]


# --------------------------------------------------------------------------- #
# notify
# --------------------------------------------------------------------------- #
def test_build_keyboard_callback_data():
    kb = notify.build_keyboard("123")
    flat = [b for row in kb["inline_keyboard"] for b in row]
    datas = {b["callback_data"] for b in flat}
    assert datas == {"approve:123", "decline:123", "answer:123"}


def test_parse_callback():
    assert notify.parse_callback("approve:123") == ("approve", "123")
    assert notify.parse_callback("") == ("", "")


def test_build_message_contains_link_and_instruction():
    msg = notify.build_message(
        "55", "Пошаговая инструкция", subject="Тема", sources=[{"title": "A", "source": "b"}]
    )
    assert "Тикет #55" in msg
    assert "Тема" in msg
    assert "Пошаговая инструкция" in msg
    assert "Источники" in msg


def test_build_message_truncates_long_instruction():
    msg = notify.build_message("1", "x" * 10000)
    assert len(msg) <= notify._MAX_TEXT + 200
    assert msg.endswith("…")


class FakeBot:
    def __init__(self):
        self.sent = None

    def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        return {"message_id": 4321}


def test_send_for_review_persists_pending(temp_db, monkeypatch):
    monkeypatch.setattr(config.telegram, "chat_id", "-100999")
    bot = FakeBot()
    mid = notify.send_for_review(
        ticket_id="88",
        instruction="инструкция",
        ticket_text="текст тикета",
        subject="тема",
        sources=[{"title": "T", "source": "s"}],
        model="deepseek",
        client=bot,
    )
    assert mid == 4321
    assert bot.sent["chat_id"] == "-100999"
    row = store.get("88")
    assert row["status"] == store.STATUS_PENDING
    assert row["message_id"] == 4321
    assert row["ai_model"] == "deepseek"


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def test_client_surfaces_telegram_error(monkeypatch):
    req = httpx.Request("POST", "http://x")
    resp = httpx.Response(
        400, json={"ok": False, "description": "Bad Request: chat not found"}, request=req
    )
    monkeypatch.setattr(tg_client.httpx, "post", lambda *a, **k: resp)
    bot = tg_client.BotClient(token="dummy")
    with pytest.raises(tg_client.TelegramError, match="chat not found"):
        bot.send_message(chat_id="123", text="hi")


def test_is_allowed(monkeypatch):
    from tgbot.bot import is_allowed

    monkeypatch.setattr(config.telegram, "allowed_users", ())
    assert is_allowed({"id": 1}) is True  # пустой список = всем

    monkeypatch.setattr(config.telegram, "allowed_users", ("123", "ivan"))
    assert is_allowed({"id": 123}) is True                       # по id
    assert is_allowed({"id": 999, "username": "Ivan"}) is True   # по username (без регистра)
    assert is_allowed({"id": 999, "username": "bob"}) is False
    assert is_allowed({"id": 999}) is False


def test_build_ticket_picker():
    items = [
        {"id": "110773", "subject": "Нет связи"},
        {"ticket_id": "110774", "subject": ""},  # принимает и id, и ticket_id
    ]
    kb = commands.build_ticket_picker(items, commands.ACTION_PICK)
    flat = [b for row in kb["inline_keyboard"] for b in row]
    assert [b["callback_data"] for b in flat] == ["pick:110773", "pick:110774"]
    assert "Нет связи" in flat[0]["text"]
    # тем же парсером
    assert notify.parse_callback("open:110773") == ("open", "110773")


def test_format_fetch_summary_variants():
    out = commands.format_fetch_summary(
        {"found": 3, "sent": ["1", "2"], "skipped": ["3"], "empty": []}
    )
    assert "найдено: 3" in out and "#1" in out and "Уже на ревью" in out
    empty = commands.format_fetch_summary(
        {"found": 0, "sent": [], "skipped": [], "empty": []}
    )
    assert "Новых тикетов нет" in empty


def test_parse_command():
    assert commands.parse_command("/ticket 110773") == ("ticket", "110773")
    assert commands.parse_command("/stats@MyBot") == ("stats", "")
    assert commands.parse_command("/HELP") == ("help", "")
    assert commands.parse_command("обычный текст") is None
    assert commands.parse_command(None) is None


def test_stats_counts(temp_db):
    store.create_pending("1", "-100", 1, "t1", "i1")
    store.create_pending("2", "-100", 2, "t2", "i2")
    store.create_pending("3", "-100", 3, "t3", "i3")
    store.record_decision("1", store.STATUS_APPROVE)
    store.set_operator_answer("2", "ответ")

    s = store.stats()
    assert s["total"] == 3
    assert s["by_status"][store.STATUS_APPROVE] == 1
    assert s["by_status"][store.STATUS_EDITED] == 1
    assert s["by_status"][store.STATUS_PENDING] == 1

    out = commands.format_stats(s)
    assert "Всего тикетов" in out
    assert "Обработано: 2/3" in out


def test_format_pending(temp_db):
    store.create_pending("5", "-100", 1, "txt", "instr", subject="Нет связи")
    out = commands.format_pending(store.iter_reviews([store.STATUS_PENDING]))
    assert "#5" in out and "Нет связи" in out
    assert commands.format_pending([]).startswith("Нет тикетов")


def test_format_ticket(temp_db):
    store.create_pending("7", "-100", 1, "текст", "инструкция ИИ", subject="Тема")
    store.set_operator_answer("7", "ответ оператора", 1, "@op")
    out = commands.format_ticket(store.get("7"))
    assert "Тикет #7" in out
    assert "Свой ответ" in out
    assert "инструкция ИИ" in out
    assert "ответ оператора" in out
    assert commands.format_ticket(None) == "Тикет не найден в базе ревью."
