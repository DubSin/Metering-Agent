"""Тесты поллера HelpDesk — без RAG/сети."""
import threading

import poller
from config import config


def test_poll_once_uses_config(monkeypatch):
    calls = {}
    monkeypatch.setattr(config, "fetch_statuses", "open")
    monkeypatch.setattr(config, "fetch_limit", 7)

    def fake_fetch(statuses=None, limit=None):
        calls["statuses"] = statuses
        calls["limit"] = limit
        return {"found": 2, "sent": ["1"], "skipped": ["2"], "empty": []}

    monkeypatch.setattr(poller, "fetch_and_review", fake_fetch)

    summary = poller.poll_once()
    assert calls == {"statuses": "open", "limit": 7}   # берёт значения из config
    assert summary["sent"] == ["1"]


def test_run_stops_on_event(monkeypatch):
    """Цикл делает проход и завершается по stop-событию, не зависая на интервале."""
    monkeypatch.setattr(config, "poll_interval", 0)
    stop = threading.Event()
    passes = []

    def fake_once():
        passes.append(1)
        stop.set()            # после первого прохода просим остановиться
        return {}

    monkeypatch.setattr(poller, "poll_once", fake_once)
    poller.run(stop)          # не должен зависнуть
    assert passes == [1]


def test_run_survives_failed_pass(monkeypatch):
    """Падение прохода логируется и не роняет цикл."""
    monkeypatch.setattr(config, "poll_interval", 0)
    stop = threading.Event()
    passes = []

    def boom():
        passes.append(1)
        if len(passes) >= 2:
            stop.set()
        raise RuntimeError("HelpDesk недоступен")

    monkeypatch.setattr(poller, "poll_once", boom)
    poller.run(stop)          # исключение проглатывается, цикл продолжается
    assert len(passes) == 2
