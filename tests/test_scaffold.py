import sqlite3
from pathlib import Path

import pytest

from uni_agent import db
from uni_agent.config import load_settings


def test_schema_creates_all_tables():
    conn = db.connect(":memory:")
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"items", "documents", "alerts_sent", "quiz_questions", "quiz_log", "llm_usage", "kv"} <= names


def test_document_kind_is_constrained():
    conn = db.connect(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO documents(path, sha256, kind, ingested_at) VALUES ('x', 'h', 'tutorial', 'now')"
        )


def test_kv_roundtrip():
    conn = db.connect(":memory:")
    db.kv_set(conn, "a", "1")
    db.kv_set(conn, "a", "2")
    assert db.kv_get(conn, "a") == "2"
    assert db.kv_get(conn, "missing") is None


def test_settings_load(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("timezone: Asia/Dubai\ncourse_folders: [C:/x/205]\ncourse_names: {'205': CSIT205}\n")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    s = load_settings(cfg, tmp_path / "missing.env")
    assert s.secrets.telegram_chat_id == 12345
    assert s.course_name(s.course_folders[0]) == "CSIT205"
    assert str(s.tz) == "Asia/Dubai"


def test_single_instance_lock(tmp_path: Path):
    from uni_agent.main import acquire_single_instance_lock

    lock_path = tmp_path / "uni_agent.lock"
    first = acquire_single_instance_lock(lock_path)
    assert first is not None
    assert acquire_single_instance_lock(lock_path) is None  # a second agent must not start
    first.close()
    again = acquire_single_instance_lock(lock_path)
    assert again is not None  # released when the first process ends
    again.close()
