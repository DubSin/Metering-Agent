"""
SQLite-хранилище решений операторов — датасет для дообучения модели.

Две таблицы:
  reviews        — по одному тикету: предложенный ИИ-ответ + решение оператора.
  answer_prompts — связь сообщения-приглашения (ForceReply) с тикетом, чтобы
                   поймать текстовый ответ оператора по reply_to_message_id.

Webhook-процесс пишет pending, bot-процесс обновляет решения — оба ходят в одну БД,
поэтому включён WAL (выдерживает конкурентных писателей).
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from config import config

# Допустимые статусы ревью
STATUS_PENDING = "pending"
STATUS_APPROVE = "approve"
STATUS_DECLINE = "decline"
STATUS_EDITED = "edited"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    ticket_id       TEXT PRIMARY KEY,
    chat_id         TEXT,
    message_id      INTEGER,
    subject         TEXT,
    ticket_text     TEXT NOT NULL,
    ai_instruction  TEXT,
    ai_sources      TEXT,           -- json
    ai_model        TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    operator_id     TEXT,
    operator_name   TEXT,
    operator_answer TEXT,
    created_at      REAL NOT NULL,
    decided_at      REAL
);

CREATE TABLE IF NOT EXISTS answer_prompts (
    prompt_message_id INTEGER PRIMARY KEY,
    ticket_id         TEXT NOT NULL,
    created_at        REAL NOT NULL
);

-- Все сообщения с action-кнопками по тикету (рассылка + карточки /ticket, /pending),
-- чтобы при решении погасить markup сразу во всех.
CREATE TABLE IF NOT EXISTS ticket_messages (
    chat_id    TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    ticket_id  TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
"""


def _db_path() -> Path:
    p = Path(config.feedback_db)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# reviews
# --------------------------------------------------------------------------- #
def create_pending(
    ticket_id: str,
    chat_id: str | int,
    message_id: int,
    ticket_text: str,
    ai_instruction: str,
    ai_sources: list[dict] | None = None,
    ai_model: str | None = None,
    subject: str | None = None,
) -> None:
    """Зарегистрировать тикет, отправленный на ревью (idempotent по ticket_id)."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO reviews (
                ticket_id, chat_id, message_id, subject, ticket_text,
                ai_instruction, ai_sources, ai_model, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                message_id=excluded.message_id,
                subject=excluded.subject,
                ticket_text=excluded.ticket_text,
                ai_instruction=excluded.ai_instruction,
                ai_sources=excluded.ai_sources,
                ai_model=excluded.ai_model,
                status='pending',
                operator_id=NULL,
                operator_name=NULL,
                operator_answer=NULL,
                decided_at=NULL,
                created_at=excluded.created_at
            """,
            (
                str(ticket_id),
                str(chat_id),
                int(message_id),
                subject,
                ticket_text,
                ai_instruction,
                json.dumps(ai_sources or [], ensure_ascii=False),
                ai_model,
                STATUS_PENDING,
                time.time(),
            ),
        )


def record_decision(
    ticket_id: str,
    status: str,
    operator_id: str | int | None = None,
    operator_name: str | None = None,
) -> bool:
    """Зафиксировать approve/decline. Возвращает True, если строка нашлась."""
    assert status in (STATUS_APPROVE, STATUS_DECLINE)
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE reviews
               SET status=?, operator_id=?, operator_name=?, decided_at=?
             WHERE ticket_id=?
            """,
            (status, _s(operator_id), operator_name, time.time(), str(ticket_id)),
        )
        return cur.rowcount > 0


def set_operator_answer(
    ticket_id: str,
    answer: str,
    operator_id: str | int | None = None,
    operator_name: str | None = None,
) -> bool:
    """Сохранить собственный ответ оператора (status=edited)."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE reviews
               SET status=?, operator_answer=?, operator_id=?,
                   operator_name=?, decided_at=?
             WHERE ticket_id=?
            """,
            (
                STATUS_EDITED,
                answer,
                _s(operator_id),
                operator_name,
                time.time(),
                str(ticket_id),
            ),
        )
        return cur.rowcount > 0


def get(ticket_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM reviews WHERE ticket_id=?", (str(ticket_id),)
        ).fetchone()
        return dict(row) if row else None


def exists(ticket_id: str | int) -> bool:
    """Уже есть ли ревью по этому тикету (для дедупа в поллере)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM reviews WHERE ticket_id=?", (str(ticket_id),)
        ).fetchone()
        return row is not None


def stats() -> dict:
    """Сводка по статусам ревью: {total, by_status: {status: count}}."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM reviews GROUP BY status"
        ).fetchall()
    by_status = {r["status"]: r["c"] for r in rows}
    return {"total": sum(by_status.values()), "by_status": by_status}


def iter_reviews(statuses: list[str] | None = None) -> list[dict]:
    """Все ревью (опц. с фильтром по статусам), новые — первыми."""
    with _connect() as conn:
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            rows = conn.execute(
                f"SELECT * FROM reviews WHERE status IN ({placeholders}) "
                "ORDER BY created_at DESC",
                statuses,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reviews ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# answer_prompts (ForceReply)
# --------------------------------------------------------------------------- #
def link_answer_prompt(prompt_message_id: int, ticket_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO answer_prompts "
            "(prompt_message_id, ticket_id, created_at) VALUES (?, ?, ?)",
            (int(prompt_message_id), str(ticket_id), time.time()),
        )


def resolve_answer_prompt(prompt_message_id: int) -> str | None:
    """Найти тикет по сообщению-приглашению и убрать запись (one-shot)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT ticket_id FROM answer_prompts WHERE prompt_message_id=?",
            (int(prompt_message_id),),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "DELETE FROM answer_prompts WHERE prompt_message_id=?",
            (int(prompt_message_id),),
        )
        return row["ticket_id"]


def add_ticket_message(ticket_id: str, chat_id: str | int, message_id: int) -> None:
    """Зарегистрировать сообщение с action-кнопками по тикету."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ticket_messages "
            "(chat_id, message_id, ticket_id, created_at) VALUES (?, ?, ?, ?)",
            (str(chat_id), int(message_id), str(ticket_id), time.time()),
        )


def get_ticket_messages(ticket_id: str) -> list[dict]:
    """Все (chat_id, message_id) с активными кнопками по тикету."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, message_id FROM ticket_messages WHERE ticket_id=?",
            (str(ticket_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_ticket_messages(ticket_id: str) -> None:
    """Снять регистрацию сообщений по тикету (после решения)."""
    with _connect() as conn:
        conn.execute("DELETE FROM ticket_messages WHERE ticket_id=?", (str(ticket_id),))


def _s(v: str | int | None) -> str | None:
    return str(v) if v is not None else None
