"""Ingest: Moodle course files -> the matching course folder's Moodle/ subfolder (read-only toward Moodle).

Downloads go to data/moodle_tmp first, are hashed, and only then moved into the course folder, so:
- content you already have (e.g. downloaded by hand) is never saved twice;
- on the FIRST sync, files are recorded as already-seen *before* they appear, so the folder watcher
  doesn't summarise the whole semester at once (free-tier friendly). Later new/updated files get summarised.
No Telegram here; the orchestrator reports the result."""
from __future__ import annotations

import fnmatch
import logging
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from ..config import Settings
from ..extract import SUPPORTED, sha256_file
from ..sources.folder_watch import guess_kind
from ..sources.moodle_ical import clean_course
from ..sources.moodle_ws import MoodleWS, MoodleWSError, list_files
from . import ingest

log = logging.getLogger(__name__)

BASELINE_KEY = "moodle_files_baseline"


@dataclass
class SyncResult:
    first_run: bool = False
    new_files: list[str] = field(default_factory=list)  # "CSIT205: L03.pdf"
    duplicates: int = 0
    skipped_courses: list[str] = field(default_factory=list)
    errors: int = 0


def folder_for_course(settings: Settings, code: str) -> Path | None:
    """'CSIT205' -> the course folder whose name maps to it in course_names (e.g. .../UOWD/205)."""
    for folder in settings.course_folders:
        if settings.course_name(folder).upper() == code.upper():
            return folder
    return None


def _target_dir(course_folder: Path, module: str, filename: str) -> Path:
    kind = guess_kind(course_folder / f"{module} {Path(filename).stem}{Path(filename).suffix}", course_folder)
    sub = {"lecture": "lectures", "lab": "labs"}.get(kind, "")
    return course_folder / "Moodle" / sub if sub else course_folder / "Moodle"


def _free_name(folder: Path, filename: str) -> Path:
    dest = folder / filename
    n = 2
    while dest.exists():
        dest = folder / f"{Path(filename).stem} ({n}){Path(filename).suffix}"
        n += 1
    return dest


async def sync_files(conn: sqlite3.Connection, ws: MoodleWS, settings: Settings) -> SyncResult:
    result = SyncResult(first_run=db.kv_get(conn, BASELINE_KEY) is None)
    userid = (await ws.call("core_webservice_get_site_info"))["userid"]
    courses = await ws.call("core_enrol_get_users_courses", userid=userid)
    tmp_dir = settings.data_dir / "moodle_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.moodle_max_file_mb * 1024 * 1024

    for course in courses:
        code = clean_course(str(course.get("shortname") or ""))
        folder = folder_for_course(settings, code)
        if folder is None or not folder.is_dir():
            result.skipped_courses.append(code)
            continue
        try:
            contents = await ws.call("core_course_get_contents", courseid=course["id"])
        except MoodleWSError as e:
            if e.permanent:
                raise
            log.warning("Moodle contents for %s failed (%s)", code, e.code)
            result.errors += 1
            continue

        for f in list_files(contents):
            ext = Path(f.filename).suffix.lower()
            if ext not in SUPPORTED or f.filesize > max_bytes:
                continue
            if any(fnmatch.fnmatch(f.filename.lower(), p.lower()) for p in settings.ignore_patterns):
                continue
            row = conn.execute("SELECT timemodified FROM moodle_files WHERE course_id = ? AND fileurl = ?",
                               (course["id"], f.fileurl)).fetchone()
            if row and row["timemodified"] >= f.timemodified:
                continue  # already have this version

            tmp = tmp_dir / f"{uuid.uuid4().hex}{ext}"
            try:
                await ws.download(f.fileurl, tmp, max_bytes)
            except MoodleWSError as e:
                if e.permanent:
                    raise
                log.warning("Moodle download failed for %s (%s)", f.filename, e.code)
                tmp.unlink(missing_ok=True)
                result.errors += 1
                continue

            sha = sha256_file(tmp)
            local: Path | None = None
            if ingest.known_document(conn, sha):
                tmp.unlink()  # same content already in your folders: don't duplicate it
                result.duplicates += 1
            else:
                target = _target_dir(folder, f.module, f.filename)
                target.mkdir(parents=True, exist_ok=True)
                local = _free_name(target, f.filename)
                if result.first_run:  # record before it appears, so the watcher treats it as already seen
                    ingest.record_document(conn, local, sha, guess_kind(local, folder), code, None, status="baseline")
                shutil.move(str(tmp), str(local))
                result.new_files.append(f"{code}: {local.name}")
            with conn:
                conn.execute(
                    "INSERT INTO moodle_files(course_id, fileurl, timemodified, sha256, local_path, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(course_id, fileurl) DO UPDATE SET "
                    "timemodified = excluded.timemodified, sha256 = excluded.sha256, "
                    "local_path = COALESCE(excluded.local_path, local_path), synced_at = excluded.synced_at",
                    (course["id"], f.fileurl, f.timemodified, sha, str(local) if local else None,
                     db.iso(db.utcnow())),
                )

    if result.first_run:
        db.kv_set(conn, BASELINE_KEY, db.iso(db.utcnow()))
    db.kv_set(conn, "last_moodle_file_sync", db.iso(db.utcnow()))
    return result
