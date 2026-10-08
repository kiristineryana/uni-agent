"""Live record of the files in the course folders: read from disk on every request (so renames and deletions
show up immediately), joined with what the agent knows about each file. No Telegram here."""
from __future__ import annotations

import fnmatch
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import Settings
from ..extract import SUPPORTED, sha256_file
from ..sources.folder_watch import TEMP_PATTERNS, guess_kind

STATUS = {
    "done": "✅ summarised",
    "baseline": "📦 here before the bot",
    "pending_kind": "❓ waiting: Lecture or Lab?",
    "queued": "⏳ being summarised",
    "queued_existing": "⏳ being summarised",
    "no_text": "⚠️ no readable text",
    "error": "⚠️ couldn't be read",
}
KIND_ICON = {"lecture": "📘", "lab": "🧪"}
PER_COURSE_LIMIT = 15

_hash_cache: dict[tuple[str, int, int], str] = {}  # (path, size, mtime) -> sha256, so big files hash once


@dataclass
class FileEntry:
    rel: str
    kind: str | None
    status: str
    modified: datetime


def _sha(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _hash_cache:
        _hash_cache[key] = sha256_file(path)
    return _hash_cache[key]


def course_entries(conn: sqlite3.Connection, settings: Settings, folder: Path) -> list[FileEntry]:
    entries = []
    for p in folder.rglob("*"):
        if not p.is_file() or any(fnmatch.fnmatch(p.name.lower(), t.lower()) for t in TEMP_PATTERNS):
            continue
        modified = datetime.fromtimestamp(p.stat().st_mtime).astimezone()
        rel = str(p.relative_to(folder))
        if any(fnmatch.fnmatch(p.name.lower(), pat.lower()) for pat in settings.ignore_patterns):
            entries.append(FileEntry(rel, None, "🙈 ignored (your own work)", modified))
            continue
        if p.suffix.lower() not in SUPPORTED:
            entries.append(FileEntry(rel, None, "📄 not a readable type", modified))
            continue
        try:
            row = conn.execute("SELECT kind, status FROM documents WHERE sha256 = ?", (_sha(p),)).fetchone()
        except OSError:  # locked/being written
            entries.append(FileEntry(rel, None, "⏳ being saved", modified))
            continue
        if row is None:
            entries.append(FileEntry(rel, guess_kind(p, folder), "🆕 new, picked up on the next scan", modified))
        else:
            entries.append(FileEntry(rel, row["kind"] or guess_kind(p, folder),
                                     STATUS.get(row["status"], row["status"]), modified))
    entries.sort(key=lambda e: e.modified, reverse=True)
    return entries


def _course_block(settings: Settings, folder: Path, entries: list[FileEntry], tz: ZoneInfo, limit: int | None) -> str:
    name = settings.course_name(folder)
    summarised = sum(e.status.startswith("✅") for e in entries)
    lines = [f"📁 {name} ({folder.parent.name}\\{folder.name}) · {len(entries)} files, {summarised} summarised"]
    shown = entries if limit is None else entries[:limit]
    for e in shown:
        icon = KIND_ICON.get(e.kind, "📄")
        lines.append(f"{icon} {e.rel}\n    {e.status} · {e.modified.astimezone(tz).strftime('%d %b')}")
    if limit is not None and len(entries) > limit:
        key = next((k for k, v in settings.course_names.items() if v == name), folder.name)
        lines.append(f"… and {len(entries) - limit} more: /files {key}")
    if not entries:
        lines.append("(empty)")
    return "\n".join(lines)


SUMMARISABLE = ("baseline", "no_text", "error")  # on disk, known, but no summary yet


def summarisable(conn: sqlite3.Connection, settings: Settings, course: str | None = None) -> list[sqlite3.Row]:
    """Docs that can be summarised on request: still on disk (at their recorded path) and never summarised."""
    rows = conn.execute(
        f"SELECT * FROM documents WHERE status IN ({','.join('?' * len(SUMMARISABLE))}) ORDER BY ingested_at DESC",
        SUMMARISABLE).fetchall()
    folders = {str(f.resolve()).lower() for f in settings.course_folders}
    out = []
    for r in rows:
        p = Path(r["path"])
        if not p.exists() or not any(str(p.resolve()).lower().startswith(f) for f in folders):
            continue  # moved/deleted, or an upload rather than a course-folder file
        if any(fnmatch.fnmatch(p.name.lower(), pat.lower()) for pat in settings.ignore_patterns):
            continue
        if course and (r["course"] or "").upper() != course.upper():
            continue
        out.append(r)
    return out


def files_text(conn: sqlite3.Connection, settings: Settings, tz: ZoneInfo, course: str | None = None) -> str:
    """All courses (newest files first, capped per course), or one course in full when `course` is given."""
    folders = [f for f in settings.course_folders if f.is_dir()]
    if course:
        folders = [f for f in folders if settings.course_name(f).upper() == course.upper()
                   or f.name.upper() == course.upper()]
        if not folders:
            known = ", ".join(settings.course_name(f) for f in settings.course_folders)
            return f"No course folder for '{course}'. Tracked: {known}"
    blocks = [_course_block(settings, f, course_entries(conn, settings, f), tz, None if course else PER_COURSE_LIMIT)
              for f in folders]
    return "\n\n".join(blocks) if blocks else "No course folders found (check course_folders in config.yaml)."
