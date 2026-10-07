"""Routes commands and events to sub-agents. The only place that knows which agent does what."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import db, notify
from .agents import deadlines, ingest, lab, lecture
from .config import Settings
from .extract import extract_text, has_real_text, sha256_file
from .llm import LLM, LLMError
from .sources import folder_watch, moodle_ical

log = logging.getLogger(__name__)

AGENTS = {"lecture": lecture, "lab": lab}

HELP_TEXT = """🎓 Uni Agent

/deadlines – what's due in the next 14 days
/lecture [course] – then send a file: lecture summary
/lab [course] – then send a file: lab briefing
  (e.g. /lab 235, or put "/lab 235" in the file's caption)
/quiz lectures <topic> – theory quiz
/quiz labs <topic> – practical/viva quiz
/stop – end a quiz
/week – weekly review now
/status – sync time, docs, next alerts, LLM usage
/sync – pull Moodle deadlines now
/fakedeadline <min> – test alert (fake item due in <min> minutes)
/help – this message

Send a file with no command and I'll ask whether it's a lecture or a lab."""


def is_allowed(chat_id: int | None, allowed_chat_id: int | None) -> bool:
    return chat_id is not None and allowed_chat_id is not None and chat_id == allowed_chat_id


class Orchestrator:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, llm: LLM | None = None):
        self.settings = settings
        self.conn = conn
        self.llm = llm
        self._doc_lock = asyncio.Lock()  # one LLM job at a time (free-tier rate limits)
        self._failed_notified: set[str] = set()

    # --- documents -------------------------------------------------------

    def baseline_folders(self) -> int:
        """First sight of a course folder: record its existing files as seen (no summaries)."""
        marked = 0
        for folder in self.settings.course_folders:
            key = f"baseline:{folder.resolve()}"
            if self.settings.backfill_existing or db.kv_get(self.conn, key) or not folder.is_dir():
                continue
            for f in folder_watch.scan([folder], self.settings.ignore_patterns):
                sha = sha256_file(f.path)
                if not ingest.known_document(self.conn, sha):
                    ingest.record_document(self.conn, f.path, sha, f.kind, self.settings.course_name(folder),
                                           None, status="baseline")
                    marked += 1
            db.kv_set(self.conn, key, db.iso(db.utcnow()))
        if marked:
            log.info("Baseline: marked %d existing files as seen", marked)
        return marked

    async def scan_folders(self) -> None:
        """Catch anything the watcher missed (e.g. files added while the app was off)."""
        found = await asyncio.to_thread(folder_watch.scan, self.settings.course_folders, self.settings.ignore_patterns)
        for f in found:
            await self.on_new_file(f)

    async def on_new_file(self, f: folder_watch.FoundFile) -> None:
        """Route a file from a watched folder: lectures -> Lecture agent, labs -> Lab agent, unsure -> ask."""
        sha = await asyncio.to_thread(sha256_file, f.path)
        if ingest.known_document(self.conn, sha):
            return
        course = self.settings.course_name(f.course_folder)
        if f.kind is None:
            await self.ask_kind(f.path, course, sha)
        else:
            await self.process_document(f.path, f.kind, course, sha)

    async def ask_kind(self, path: Path, course: str, sha: str) -> None:
        """Record the file as waiting for a decision and ask with two buttons. Survives restarts."""
        ingest.record_document(self.conn, path, sha, None, course, None, status="pending_kind")
        doc_id = ingest.known_document(self.conn, sha)["id"]
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("📘 Lecture", callback_data=f"kind:{doc_id}:lecture"),
            InlineKeyboardButton("🧪 Lab", callback_data=f"kind:{doc_id}:lab"),
        ]])
        await notify.send_telegram(f"📄 {path.name} ({course})\nLecture or Lab?", reply_markup=buttons)

    async def resolve_kind(self, doc_id: int, kind: str) -> str:
        """Button pressed. Returns the text to put on the question message."""
        row = self.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None or row["status"] != "pending_kind":
            return "Already handled."
        path = Path(row["path"])
        if not path.exists():
            return f"{path.name} no longer exists."
        with self.conn:  # a second press of either button is now "Already handled"
            self.conn.execute("UPDATE documents SET status = 'queued' WHERE id = ?", (doc_id,))
        asyncio.create_task(self.process_document(path, kind, row["course"], row["sha256"], from_upload=True))
        return f"{path.name} → {'📘 Lecture' if kind == 'lecture' else '🧪 Lab'}. Working on it…"

    async def process_upload(self, path: Path, kind: str | None, course: str | None = None) -> None:
        """A file sent in Telegram. kind=None means no command was given, so ask."""
        sha = await asyncio.to_thread(sha256_file, path)
        course = course or ingest.guess_course(path.name, self.settings.course_names) or "Uploads"
        row = ingest.known_document(self.conn, sha)
        if kind and row and row["summary_path"] and row["kind"] == kind and Path(row["summary_path"]).exists():
            await notify.send_telegram("Already summarised; here it is again:\n\n"
                                       + Path(row["summary_path"]).read_text(encoding="utf-8"))
            return
        if kind is None:
            await self.ask_kind(path, course, sha)
        else:
            await self.process_document(path, kind, course, sha, from_upload=True)

    def course_from_arg(self, arg: str | None) -> str | None:
        """'/lecture 205' or '/lecture CSIT205' -> 'CSIT205'."""
        if not arg:
            return None
        arg = arg.strip().upper()
        for key, name in self.settings.course_names.items():
            if arg in (key.upper(), name.upper()):
                return name
        return arg

    def _lab_deadlines(self, course: str) -> list[str]:
        items = deadlines.upcoming(self.conn, db.utcnow(), timedelta(days=21))
        return [f"{i.title}: {deadlines.fmt_due(i.due_at, self.settings.tz)}"
                for i in items if i.course == course and deadlines.kind_tag(i) == "🧪 "]

    async def process_document(self, path: Path, kind: str, course: str, sha: str,
                               from_upload: bool = False) -> None:
        if kind not in AGENTS:
            raise ValueError(f"unknown kind {kind!r}")
        if self.llm is None:
            await notify.send_telegram(f"Can't summarise {path.name}: LLM_API_KEY is not set.")
            return
        title = ingest.title_from_path(path)
        label = "lecture" if kind == "lecture" else "lab sheet"
        async with self._doc_lock:
            row = ingest.known_document(self.conn, sha)
            if not from_upload and row and row["status"] not in ("pending_kind", "queued"):
                return  # the watcher and the periodic scan both saw it; the first one handled it
            try:
                text = await asyncio.to_thread(extract_text, path)
                if not has_real_text(text):
                    await notify.send_telegram(
                        f"⚠️ {path.name}: no readable text (scanned PDF or image-only slides?). Skipped.")
                    ingest.record_document(self.conn, path, sha, kind, course, None, status="no_text")
                    return
                if not from_upload:
                    await notify.send_telegram(f"📥 New {label} found: {title} ({course}). Working on it…")
                earlier = ingest.earlier_titles(self.conn, kind, course)
                max_chars = self.settings.llm.max_single_request_chars
                if kind == "lab":
                    summary = await asyncio.to_thread(lab.summarize, self.llm, text, title, course, earlier,
                                                      max_chars, self._lab_deadlines(course))
                else:
                    summary = await asyncio.to_thread(lecture.summarize, self.llm, text, title, course, earlier,
                                                      max_chars)
            except LLMError as e:
                log.warning("Summary failed for %s: %s", path.name, e)
                with self.conn:  # re-arm the Lecture/Lab buttons
                    self.conn.execute("UPDATE documents SET status = 'pending_kind' "
                                      "WHERE sha256 = ? AND status = 'queued'", (sha,))
                if sha not in self._failed_notified:
                    self._failed_notified.add(sha)
                    retry = ("Press the button again or resend it later." if from_upload
                             else "I'll retry on the next scan.")
                    await notify.send_telegram(f"⚠️ Couldn't summarise {path.name}: {e} {retry}")
                return
            except Exception as e:
                log.exception("Processing failed for %s", path.name)
                await notify.send_telegram(f"⚠️ Couldn't read {path.name} ({type(e).__name__}). Skipped.")
                ingest.record_document(self.conn, path, sha, kind, course, None, status="error")
                return

            out = ingest.summary_file(self.settings.data_dir, kind, course, path, sha)
            out.write_text(summary + f"\n\n---\nSource: {path}\n", encoding="utf-8")
            ingest.record_document(self.conn, path, sha, kind, course, out)
            self._failed_notified.discard(sha)
        await notify.send_telegram(summary)

    def help(self) -> str:
        return HELP_TEXT

    # --- deadlines -------------------------------------------------------

    def deadlines(self) -> str:
        now = db.utcnow()
        days = self.settings.deadlines_window_days
        items = deadlines.upcoming(self.conn, now, timedelta(days=days))
        return deadlines.deadlines_text(items, now, self.settings.tz, days)

    async def run_ingest(self) -> None:
        await self.run_ical_sync()
        await self.scan_folders()

    async def run_ical_sync(self) -> str:
        url = self.settings.secrets.moodle_ical_url
        if not url:
            return "Moodle feed not configured (MOODLE_ICAL_URL)."
        try:
            raw = await moodle_ical.fetch(url.get_secret_value())
            items = moodle_ical.parse(raw, self.settings.tz, self.settings.deadline_ignore)
        except Exception as e:  # FeedError, or a parse error on a malformed feed
            msg = str(e) if isinstance(e, moodle_ical.FeedError) else f"could not parse feed ({type(e).__name__})"
            log.warning("iCal sync failed: %s", msg)
            return f"Moodle sync failed: {msg}"
        added, updated = ingest.upsert_items(self.conn, items)
        db.kv_set(self.conn, "last_ical_sync", db.iso(db.utcnow()))
        log.info("iCal sync: %d events, %d new, %d updated", len(items), added, updated)
        return f"Moodle sync OK: {len(items)} events ({added} new, {updated} updated)."

    async def run_deadline_check(self) -> int:
        now = db.utcnow()
        sent = 0
        for item, labels in deadlines.pending_alerts(self.conn, now, self.settings.alert_offsets):
            await notify.send_telegram(deadlines.alert_text(item, now, self.settings.tz))
            deadlines.mark_sent(self.conn, item.id, labels, now)
            sent += 1
        return sent

    async def add_fake_deadline(self, minutes: int) -> str:
        due = db.utcnow() + timedelta(minutes=minutes)
        ingest.add_manual_item(self.conn, f"Fake test assignment ({minutes} min)", "TEST", due, source="fake")
        await self.run_deadline_check()
        return f"Added a fake deadline due {deadlines.fmt_due(due, self.settings.tz)} and ran the alert check."

    def _fmt_local(self, iso_utc: str | None) -> str:
        if not iso_utc:
            return "never"
        return datetime.fromisoformat(iso_utc).astimezone(self.settings.tz).strftime("%a %d %b, %H:%M")

    def status(self) -> str:
        c = self.conn
        today = db.utcnow().astimezone(self.settings.tz).date().isoformat()
        usage = c.execute("SELECT tokens_in, tokens_out FROM llm_usage WHERE day = ?", (today,)).fetchone()
        used = (usage["tokens_in"] + usage["tokens_out"]) if usage else 0
        docs = c.execute(
            "SELECT kind, COUNT(*) n FROM documents WHERE summary_path IS NOT NULL GROUP BY kind"
        ).fetchall()
        doc_counts = {r["kind"]: r["n"] for r in docs}
        upcoming = c.execute(
            "SELECT title, course, due_at FROM items WHERE status = 'open' AND due_at > ? ORDER BY due_at LIMIT 1",
            (db.iso(db.utcnow()),),
        ).fetchone()
        next_item = (
            f"{upcoming['title']} ({upcoming['course'] or '?'}) – {self._fmt_local(upcoming['due_at'])}"
            if upcoming else "nothing scheduled"
        )
        return (
            "📊 Status\n"
            f"Started: {self._fmt_local(db.kv_get(c, 'started_at'))}\n"
            f"Last Moodle sync: {self._fmt_local(db.kv_get(c, 'last_ical_sync'))}\n"
            f"Docs summarised: {doc_counts.get('lecture', 0)} lectures, {doc_counts.get('lab', 0)} labs\n"
            f"Next deadline: {next_item}\n"
            f"LLM today: {used:,} / {self.settings.llm.daily_token_cap:,} tokens "
            f"({self.settings.llm.provider}{'' if self.llm else ', no API key'})"
        )
