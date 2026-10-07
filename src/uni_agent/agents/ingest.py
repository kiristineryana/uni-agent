"""Ingest agent: pulls deadlines from Moodle into `items`. (Folder ingest arrives in Phase 3a.)"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .. import db
from ..sources.moodle_ical import FeedItem


def upsert_items(conn: sqlite3.Connection, items: list[FeedItem], source: str = "moodle_ical") -> tuple[int, int]:
    """Insert new items, update changed ones. Returns (added, updated).
    If an item's due time moves, its sent alerts are cleared so it gets re-alerted on the new schedule."""
    added = updated = 0
    with conn:
        for it in items:
            due = db.iso(it.due_at)
            row = conn.execute("SELECT id, title, course, type, due_at FROM items WHERE uid = ?", (it.uid,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO items(course, title, type, due_at, source, uid) VALUES (?, ?, ?, ?, ?, ?)",
                    (it.course, it.title, it.type, due, source, it.uid),
                )
                added += 1
            elif (row["title"], row["course"], row["type"], row["due_at"]) != (it.title, it.course, it.type, due):
                conn.execute(
                    "UPDATE items SET title = ?, course = ?, type = ?, due_at = ? WHERE id = ?",
                    (it.title, it.course, it.type, due, row["id"]),
                )
                if row["due_at"] != due:
                    conn.execute("DELETE FROM alerts_sent WHERE item_id = ?", (row["id"],))
                updated += 1
    return added, updated


def add_manual_item(conn: sqlite3.Connection, title: str, course: str | None, due_at: datetime,
                    type_: str = "assignment", source: str = "manual") -> int:
    uid = f"{source}-{db.iso(db.utcnow())}-{title}"
    with conn:
        cur = conn.execute(
            "INSERT INTO items(course, title, type, due_at, source, uid) VALUES (?, ?, ?, ?, ?, ?)",
            (course, title, type_, db.iso(due_at), source, uid),
        )
    return cur.lastrowid
