"""Routes commands and events to sub-agents. The only place that knows which agent does what."""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import db, notify
from .agents import deadlines, exam, ingest, lab, lecture, moodle_sync, weekly
from .config import Settings
from .errors import ErrorReporter
from .extract import OCR_MARK, VOICE_MARK, extract_text, has_real_text, sha256_file
from .ocr import get_ocr
from .transcribe import make_transcriber
from .llm import LLM, LLMError
from .sources import folder_watch, moodle_ical, moodle_ws

log = logging.getLogger(__name__)

AGENTS = {"lecture": lecture, "lab": lab}
ICAL_FAILURES_BEFORE_ALERT = 3


@dataclass
class QuizSession:
    kind: str
    queue: list[int]
    current: int | None = None
    asked: int = 0
    score: float = 0.0

HELP_TEXT = """🎓 Uni Agent

/deadlines – what's due in the next 14 days
/lecture [course] – then send a file: lecture summary
/lab [course] – then send a file: lab briefing
  (e.g. /lab 235, or put "/lab 235" in the file's caption)
/quiz lectures <topic> – theory quiz
/quiz labs <topic> – practical/viva quiz
/skip – skip a quiz question
/stop – end a quiz
/week – weekly review now
/status – sync time, docs, next alerts, LLM usage
/sync – pull Moodle deadlines (and files, if set up) now
/fakedeadline <min> – test alert (fake item due in <min> minutes)
/help – this message

Send a file with no command and I'll ask whether it's a lecture or a lab.
📷 Photos work too (read with OCR); send several as an album for one summary.
🎙️ Voice notes: during a quiz they're your answer; otherwise transcribed and summarised."""


def is_allowed(chat_id: int | None, allowed_chat_id: int | None) -> bool:
    return chat_id is not None and allowed_chat_id is not None and chat_id == allowed_chat_id


class Orchestrator:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, llm: LLM | None = None):
        self.settings = settings
        self.conn = conn
        self.llm = llm
        self._doc_lock = asyncio.Lock()  # one LLM job at a time (free-tier rate limits)
        self._failed_notified: set[str] = set()
        self.quiz: QuizSession | None = None
        self.errors = ErrorReporter(conn)
        self._ical_failures = 0
        self._moodle_ws_refused: str | None = None
        self.ocr = get_ocr() if settings.ocr_enabled else None
        self.transcriber = (make_transcriber(settings.voice_model, settings.data_dir / "models",
                                             settings.voice_language) if settings.voice_enabled else None)

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
        """'/lecture 205' or '/lecture CSIT205' -> 'CSIT205'. Anything not course-like -> None."""
        if not arg:
            return None
        arg = arg.strip().upper()
        for key, name in self.settings.course_names.items():
            if arg in (key.upper(), name.upper()):
                return name
        return arg if re.fullmatch(r"[A-Z]{3,4}\d{3}", arg) else None

    async def process_photos(self, images: list[Path], kind: str | None, course: str | None,
                             title: str | None = None) -> None:
        """Photos (or an album) of slides/notes -> OCR -> one text document -> the normal upload flow."""
        if self.ocr is None:
            await notify.send_telegram("Photo support needs OCR, which is off or unavailable on this computer.")
            return
        n = len(images)
        await notify.send_telegram("📷 Reading the photo…" if n == 1 else f"📷 Reading {n} photos as one document…")
        texts = [await asyncio.to_thread(self.ocr, p.read_bytes()) for p in images]
        body = "\n\n".join(f"## Photo {i}\n{OCR_MARK}\n{t.strip()}" for i, t in enumerate(texts, 1) if t.strip())
        if not has_real_text(body):
            what = "that photo" if len(images) == 1 else f"those {len(images)} photos"
            await notify.send_telegram(
                f"⚠️ I couldn't read enough text in {what}. Tips: good light, page flat and filling the frame, "
                "or send it as a File (📎) for full resolution. Handwriting is hard for OCR.")
            return
        stamp = datetime.now(self.settings.tz).strftime("%Y-%m-%d %H%M")
        name = re.sub(r'[<>:"/\\|?*]+', "", title or "").strip()[:80] or f"Photo notes {stamp}"
        txt = images[0].parent / f"{name}.txt"
        txt.write_text(body + "\n", encoding="utf-8")
        log.info("OCR'd %d photo(s) into %s (%d chars)", len(images), txt.name, len(body))
        await self.process_upload(txt, kind, course)

    async def transcribe(self, path: Path) -> str | None:
        """None when voice support is off/unavailable; '' when no speech was heard."""
        if self.transcriber is None:
            return None
        return await asyncio.to_thread(self.transcriber, path)

    def awaiting_quiz_answer(self) -> bool:
        return self.quiz is not None and self.quiz.current is not None

    async def quiz_voice_answer(self, path: Path) -> str:
        text = await self.transcribe(path)
        if text is None:
            return "Voice answers need speech recognition, which is off or unavailable on this computer."
        if not text:
            return "🎙️ I couldn't hear any speech. Try again, type your answer, or /skip."
        return f"🎙️ I heard: “{text}”\n\n" + (await self.quiz_answer(text) or "")

    async def process_voice(self, path: Path, kind: str | None, course: str | None,
                            title: str | None = None) -> None:
        """A voice note / recording -> transcript -> one text document -> the normal upload flow.
        Too short to summarise -> just send the transcript back."""
        if self.transcriber is None:
            await notify.send_telegram("Voice support needs speech recognition, which is off or unavailable.")
            return
        await notify.send_telegram("🎙️ Transcribing…")
        try:
            text = await self.transcribe(path)
        except Exception as e:  # corrupt/unsupported audio
            log.exception("Transcription failed for %s", path.name)
            await notify.send_telegram(f"⚠️ Couldn't read that audio ({type(e).__name__}).")
            return
        if not text:
            await notify.send_telegram("🎙️ I couldn't hear any speech in that recording.")
            return
        body = f"## Voice note\n{VOICE_MARK}\n{text}"
        if not has_real_text(body):
            await notify.send_telegram(f"🎙️ Transcript:\n{text}\n\n(Too short to summarise; "
                                       "record a longer explanation or send the slides instead.)")
            return
        stamp = datetime.now(self.settings.tz).strftime("%Y-%m-%d %H%M")
        name = re.sub(r'[<>:"/\\|?*]+', "", title or "").strip()[:80] or f"Voice note {stamp}"
        txt = path.parent / f"{name}.txt"
        txt.write_text(body + "\n", encoding="utf-8")
        await self.process_upload(txt, kind, course)

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
                text = await asyncio.to_thread(extract_text, path, self.ocr)
                if not has_real_text(text):
                    why = ("even OCR found no text in its images" if self.ocr
                           else "scanned PDF or image-only slides? OCR is off")
                    await notify.send_telegram(f"⚠️ {path.name}: no readable text ({why}). Skipped.")
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

    # --- quiz (Exam agent) -----------------------------------------------

    async def quiz_start(self, kind: str, topic: str | None) -> str:
        if self.llm is None:
            return "Quizzes need an LLM: LLM_API_KEY is not set."
        label = "lecture" if kind == "lecture" else "lab"
        docs = exam.matching_docs(self.conn, kind, topic)
        if not docs:
            have = exam.matching_docs(self.conn, kind, None)
            hint = ("\nI have: " + ", ".join(sorted({f"{d['course']}: {ingest.title_from_path(Path(d['path']))}"
                                                     for d in have}))) if have else \
                f"\nNo {label} summaries yet. Send one with /{label} first."
            return f"No {label} summaries match '{topic}'.{hint}"
        try:
            async with self._doc_lock:
                generated = 0
                for d in docs:  # generate once per document; at most 3 per start to spare the free tier
                    if exam.question_count(self.conn, d["id"]) or generated >= 3:
                        continue
                    summary = exam.read_summary(d["summary_path"])
                    qs = await asyncio.to_thread(exam.generate_questions, self.llm, summary, kind,
                                                 ingest.title_from_path(Path(d["path"])))
                    exam.store_questions(self.conn, d["id"], kind, qs)
                    generated += 1
        except LLMError as e:
            return f"⚠️ Couldn't prepare questions: {e}"
        ids = exam.pick_questions(self.conn, [d["id"] for d in docs], db.utcnow(), limit=10)
        if not ids:
            return "⚠️ No questions available yet. Try again later."
        self.quiz = QuizSession(kind, ids)
        style = "theory" if kind == "lecture" else "practical / viva"
        scope = f" on '{topic}'" if topic else ""
        return (f"🎯 {style.capitalize()} quiz{scope}: {len(ids)} questions from {len(docs)} "
                f"{label}{'s' if len(docs) != 1 else ''}. Reply with your answer; /skip if you don't know, "
                f"/stop to finish.\n\n" + self._next_question())

    def _next_question(self) -> str:
        q = self.quiz
        if not q.queue:
            return self._finish_quiz()
        q.current = q.queue.pop(0)
        q.asked += 1
        row = self.conn.execute("SELECT topic, question FROM quiz_questions WHERE id = ?", (q.current,)).fetchone()
        return f"Q{q.asked} · {row['topic']}\n{row['question']}"

    def _finish_quiz(self) -> str:
        q, self.quiz = self.quiz, None
        answered = q.asked - (1 if q.current is not None else 0)
        if answered <= 0:
            return "Quiz ended."
        return f"🏁 Quiz finished: {q.score:g} / {answered} ({round(100 * q.score / answered)}%)."

    async def quiz_answer(self, reply: str) -> str | None:
        """Returns None when no quiz is running (the message isn't for the Exam agent)."""
        q = self.quiz
        if q is None or q.current is None:
            return None
        row = self.conn.execute("SELECT question, answer FROM quiz_questions WHERE id = ?", (q.current,)).fetchone()
        try:
            async with self._doc_lock:
                verdict, explanation = await asyncio.to_thread(
                    exam.mark_answer, self.llm, row["question"], row["answer"], reply)
        except LLMError as e:
            return f"⚠️ Couldn't mark that ({e}). Send your answer again, /skip, or /stop."
        return self._record_and_continue(verdict, explanation, row["answer"])

    def quiz_skip(self) -> str | None:
        q = self.quiz
        if q is None or q.current is None:
            return None
        row = self.conn.execute("SELECT answer FROM quiz_questions WHERE id = ?", (q.current,)).fetchone()
        return self._record_and_continue("wrong", "Skipped; this one comes back soon.", row["answer"])

    def _record_and_continue(self, verdict: str, explanation: str, model_answer: str) -> str:
        q = self.quiz
        exam.record_answer(self.conn, q.current, verdict, db.utcnow())
        q.score += exam.SCORES[verdict]
        q.current = None
        return exam.feedback_text(verdict, explanation, model_answer) + "\n\n" + self._next_question()

    def quiz_stop(self) -> str:
        if self.quiz is None:
            return "No quiz running."
        return self._finish_quiz()  # an unanswered current question isn't counted

    # --- weekly review ---------------------------------------------------

    def weekly_review(self) -> str:
        return weekly.build_review(self.conn, db.utcnow(), self.settings.tz)

    async def run_weekly_review(self) -> None:
        await notify.send_telegram(self.weekly_review())

    def help(self) -> str:
        return HELP_TEXT

    # --- deadlines -------------------------------------------------------

    def deadlines(self) -> str:
        now = db.utcnow()
        days = self.settings.deadlines_window_days
        items = deadlines.upcoming(self.conn, now, timedelta(days=days))
        return deadlines.deadlines_text(items, now, self.settings.tz, days)

    async def run_moodle_file_sync(self) -> str:
        token = self.settings.secrets.moodle_token
        if not token:
            return "Moodle file sync not configured (MOODLE_TOKEN)."
        if self._moodle_ws_refused:
            return f"Moodle file sync is off: Moodle refused the token ({self._moodle_ws_refused})."
        base = self.settings.moodle_base_url or (
            moodle_ws.base_url_from(self.settings.secrets.moodle_ical_url.get_secret_value())
            if self.settings.secrets.moodle_ical_url else None)
        if not base:
            return "Moodle file sync needs moodle_base_url in config.yaml or MOODLE_ICAL_URL."
        ws = moodle_ws.MoodleWS(base, token.get_secret_value())
        try:
            result = await moodle_sync.sync_files(self.conn, ws, self.settings)
        except moodle_ws.MoodleWSError as e:
            if e.permanent:  # the university doesn't allow it, or the token is wrong: stop, don't work around it
                self._moodle_ws_refused = e.code
                await self.errors.report("moodle_ws", f"Moodle refused the web-service token ({e.code}). "
                                                      "File sync is off until the token is fixed; folders still work.")
            log.warning("Moodle file sync failed: %s", e.code)
            return f"Moodle file sync failed ({e.code})."
        finally:
            await ws.aclose()
        log.info("Moodle file sync: %d new, %d duplicates, %d errors, first_run=%s",
                 len(result.new_files), result.duplicates, result.errors, result.first_run)
        if result.first_run and result.new_files:
            await notify.send_telegram(
                f"📥 Moodle file sync connected: saved {len(result.new_files)} existing files into your course "
                "folders' Moodle\\ subfolders (not summarised, to save your LLM quota). "
                "New files from now on are summarised automatically. Send any with /lecture or /lab.")
        msg = f"Moodle files: {len(result.new_files)} new"
        if result.duplicates:
            msg += f", {result.duplicates} already in your folders"
        if result.errors:
            msg += f", {result.errors} failed (see log)"
        return msg + "."

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
            self._ical_failures += 1
            if self._ical_failures == ICAL_FAILURES_BEFORE_ALERT:  # ~1.5 h of failures, alert once
                await self.errors.report("ical", f"Moodle sync has failed {self._ical_failures} times in a row "
                                                 f"({msg}). Deadlines may be out of date.")
            return f"Moodle sync failed: {msg}"
        if self._ical_failures >= ICAL_FAILURES_BEFORE_ALERT:
            try:
                await notify.send_telegram("✅ Moodle sync is working again.")
            except Exception:
                pass
        self._ical_failures = 0
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
            + (f"Last Moodle file sync: {self._fmt_local(db.kv_get(c, 'last_moodle_file_sync'))}"
               f"{' (off: ' + self._moodle_ws_refused + ')' if self._moodle_ws_refused else ''}\n"
               if self.settings.secrets.moodle_token else "") +
            f"Docs summarised: {doc_counts.get('lecture', 0)} lectures, {doc_counts.get('lab', 0)} labs\n"
            f"Next deadline: {next_item}\n"
            f"LLM today: {used:,} / {self.settings.llm.daily_token_cap:,} tokens "
            f"({self.settings.llm.provider}{'' if self.llm else ', no API key'})"
        ) + self._last_error_line()

    def _last_error_line(self) -> str:
        raw = db.kv_get(self.conn, "last_error")
        if not raw:
            return ""
        when, _, text = raw.partition(" ")
        return f"\nLast problem: {self._fmt_local(when)} – {text}"
