"""SQLite schema and small helpers. All timestamps are stored as UTC ISO-8601 strings."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id      INTEGER PRIMARY KEY,
    course  TEXT,
    title   TEXT NOT NULL,
    type    TEXT,
    due_at  TEXT NOT NULL,
    source  TEXT NOT NULL,
    uid     TEXT NOT NULL UNIQUE,
    status  TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL UNIQUE,
    kind         TEXT CHECK (kind IN ('lecture', 'lab')),
    course       TEXT,
    ingested_at  TEXT NOT NULL,
    summary_path TEXT,
    status       TEXT NOT NULL DEFAULT 'done'
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    item_id      INTEGER NOT NULL REFERENCES items(id),
    offset_label TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    UNIQUE (item_id, offset_label)
);

CREATE TABLE IF NOT EXISTS quiz_questions (
    id       INTEGER PRIMARY KEY,
    doc_id   INTEGER REFERENCES documents(id),
    kind     TEXT NOT NULL CHECK (kind IN ('lecture', 'lab')),
    topic    TEXT,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL,
    box      INTEGER NOT NULL DEFAULT 1,
    next_due TEXT
);

CREATE TABLE IF NOT EXISTS quiz_log (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES quiz_questions(id),
    correct     REAL NOT NULL,
    answered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day        TEXT PRIMARY KEY,
    tokens_in  INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS moodle_files (
    course_id    INTEGER NOT NULL,
    fileurl      TEXT NOT NULL,
    timemodified INTEGER NOT NULL,
    sha256       TEXT,
    local_path   TEXT,
    synced_at    TEXT NOT NULL,
    PRIMARY KEY (course_id, fileurl)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    on_disk = str(path) != ":memory:"
    if on_disk:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if on_disk:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
