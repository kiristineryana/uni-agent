"""Ingest agent: Moodle deadlines into `items`; documents (hash/dedupe/record) into `documents`."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import db
from ..sources.moodle_ical import FeedItem


def upsert_items(conn: sqlite3.Connection, items: list[FeedItem], source: str = "moodle_ical",
                 classify: Callable[[FeedItem], tuple[str, str]] | None = None) -> tuple[int, int]:
    """Insert new items, update changed ones. Returns (added, updated).
    `classify(item) -> (status, note)` decides whether it's alerted ('open'), 'ignored' or 'unclear'; it is
    re-applied on every sync, so changing your enrolment re-filters existing items.
    If an item's due time moves, its sent alerts are cleared so it gets re-alerted on the new schedule."""
    added = updated = 0
    with conn:
        for it in items:
            due = db.iso(it.due_at)
            status, note = classify(it) if classify else ("open", None)
            row = conn.execute("SELECT id, title, course, type, due_at, status, note FROM items WHERE uid = ?",
                               (it.uid,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO items(course, title, type, due_at, source, uid, status, note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (it.course, it.title, it.type, due, source, it.uid, status, note),
                )
                added += 1
            elif (row["title"], row["course"], row["type"], row["due_at"], row["status"], row["note"]) != \
                    (it.title, it.course, it.type, due, status, note):
                conn.execute(
                    "UPDATE items SET title = ?, course = ?, type = ?, due_at = ?, status = ?, note = ? WHERE id = ?",
                    (it.title, it.course, it.type, due, status, note, row["id"]),
                )
                if row["due_at"] != due:
                    conn.execute("DELETE FROM alerts_sent WHERE item_id = ?", (row["id"],))
                updated += 1
    return added, updated


def known_document(conn: sqlite3.Connection, sha: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha,)).fetchone()


def record_document(conn: sqlite3.Connection, path: Path, sha: str, kind: str | None, course: str | None,
                    summary_path: Path | None, status: str = "done") -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO documents(path, sha256, kind, course, ingested_at, summary_path, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(sha256) DO UPDATE SET "
            "kind = excluded.kind, course = excluded.course, summary_path = excluded.summary_path, "
            "status = excluded.status, path = excluded.path, ingested_at = excluded.ingested_at",
            (str(path), sha, kind, course, db.iso(db.utcnow()), str(summary_path) if summary_path else None, status),
        )
    return cur.lastrowid


def earlier_titles(conn: sqlite3.Connection, kind: str, course: str) -> list[str]:
    rows = conn.execute(
        "SELECT path FROM documents WHERE kind = ? AND course = ? AND summary_path IS NOT NULL ORDER BY ingested_at",
        (kind, course),
    ).fetchall()
    return [title_from_path(Path(r["path"])) for r in rows]


def title_from_path(path: Path) -> str:
    return re.sub(r"[_\s]+", " ", path.stem).strip()


def summary_file(data_dir: Path, kind: str, course: str, source: Path, sha: str) -> Path:
    folder = data_dir / "summaries" / ("lectures" if kind == "lecture" else "labs") / re.sub(r"[^\w.-]+", "_", course)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{source.stem}.md"
    return out if not out.exists() else folder / f"{source.stem}-{sha[:8]}.md"


def guess_course(filename: str, course_names: dict[str, str]) -> str | None:
    """Match an uploaded file to a course by code ('CSIT205') or folder key ('205') in its name."""
    upper = filename.upper()
    for key, name in course_names.items():
        if name.upper() in upper or re.search(rf"(?<!\d){re.escape(key)}(?!\d)", upper):
            return name
    return None


def add_manual_item(conn: sqlite3.Connection, title: str, course: str | None, due_at: datetime,
                    type_: str = "assignment", source: str = "manual") -> int:
    uid = f"{source}-{db.iso(db.utcnow())}-{title}"
    with conn:
        cur = conn.execute(
            "INSERT INTO items(course, title, type, due_at, source, uid) VALUES (?, ?, ?, ?, ?, ?)",
            (course, title, type_, db.iso(due_at), source, uid),
        )
    return cur.lastrowid


def reclassify_upcoming(conn: sqlite3.Connection, classify: Callable[[FeedItem], tuple[str, str]],
                        now: datetime) -> int:
    """Re-apply the enrolment filter to stored future Moodle items, including ones no longer in the feed."""
    changed = 0
    rows = conn.execute("SELECT * FROM items WHERE source = 'moodle_ical' AND due_at > ? "
                        "AND status IN ('open', 'ignored', 'unclear')", (db.iso(now),)).fetchall()
    with conn:
        for r in rows:
            item = FeedItem(r["uid"], r["title"], r["course"], r["type"], datetime.fromisoformat(r["due_at"]))
            status, note = classify(item)
            if (status, note) != (r["status"], r["note"]):
                conn.execute("UPDATE items SET status = ?, note = ? WHERE id = ?", (status, note, r["id"]))
                changed += 1
    return changed
