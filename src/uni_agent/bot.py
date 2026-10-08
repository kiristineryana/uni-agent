"""Telegram command and file handlers. Thin: every handler delegates to the orchestrator."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters

from .extract import SUPPORTED
from .notify import split_message
from .orchestrator import Orchestrator, is_allowed

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".webp"}
AUDIO_EXT = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".flac", ".webm", ".amr"}
ALBUM_WAIT_SECONDS = 3.0  # Telegram delivers an album as separate messages a moment apart


def parse_caption(caption: str | None, course_from_arg: Callable[[str], str | None]):
    """'/lab 235 Week 3 whiteboard' -> ('lab', 'CSCI235', 'Week 3 whiteboard'); missing parts are None."""
    words = (caption or "").split()
    kind = course = None
    if words and words[0].lower() in ("/lecture", "/lab"):
        kind = words.pop(0).lower().lstrip("/")
    if words and course_from_arg(words[0]):
        course = course_from_arg(words.pop(0))
    return kind, course, " ".join(words) or None


class PhotoBatcher:
    """Collects photos into documents. A single photo is one document; an album (same media_group_id)
    becomes ONE document once no new photo has arrived for `wait` seconds and every download has finished."""

    def __init__(self, on_ready: Callable[[list[Path], dict], Awaitable[None]], make_dir: Callable[[], Path],
                 wait: float = ALBUM_WAIT_SECONDS):
        self.on_ready = on_ready
        self.make_dir = make_dir
        self.wait = wait
        self.albums: dict[str, dict] = {}
        self._tasks: set[asyncio.Task] = set()

    async def add(self, group: str | None, order: int, meta: dict,
                  download: Callable[[Path], Awaitable[Path]]) -> None:
        """`meta` = kind/course/title from this message (None = unknown); `download(dir)` saves the photo."""
        if group is None:
            path = await download(self.make_dir())
            self._spawn(self.on_ready([path], meta))
            return
        album = self.albums.get(group)
        if album is None:
            album = self.albums[group] = {"dir": self.make_dir(), "items": [], "meta": dict(meta),
                                          "downloading": 0, "last": 0.0}
            self._spawn(self._flush(group))
        else:  # captions usually sit on one photo of the album; fill in whatever is still unknown
            album["meta"] = {k: album["meta"].get(k) or v for k, v in {**album["meta"], **meta}.items()}
        loop = asyncio.get_running_loop()
        album["downloading"] += 1
        album["last"] = loop.time()
        try:
            album["items"].append((order, await download(album["dir"])))
        finally:
            album["downloading"] -= 1
            album["last"] = loop.time()

    async def _flush(self, group: str) -> None:
        loop = asyncio.get_running_loop()
        album = self.albums[group]
        while album["downloading"] or loop.time() - album["last"] < self.wait:
            await asyncio.sleep(min(0.5, self.wait))
        del self.albums[group]
        if album["items"]:
            await self.on_ready([p for _, p in sorted(album["items"])], album["meta"])

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)  # keep a reference so the task isn't garbage-collected mid-run
        task.add_done_callback(self._tasks.discard)


def register(app: Application, orch: Orchestrator, allowed_chat_id: int) -> None:
    async def guard(update: Update) -> bool:
        chat_id = update.effective_chat.id if update.effective_chat else None
        if not is_allowed(chat_id, allowed_chat_id):
            log.warning("Ignored message from unauthorised chat %s", chat_id)
            return False
        return True

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await update.message.reply_text(orch.help())

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await update.message.reply_text(orch.status())

    async def deadlines_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await update.message.reply_text(orch.deadlines())

    async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await update.message.reply_text(await orch.run_ical_sync())
            if orch.settings.secrets.moodle_token:
                await update.message.chat.send_action(ChatAction.TYPING)
                await update.message.reply_text(await orch.run_moodle_file_sync())

    async def fake_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            minutes = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
            await update.message.reply_text(await orch.add_fake_deadline(minutes))

    async def kind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
        if await guard(update):
            context.user_data["next_kind"] = kind
            context.user_data["next_course"] = orch.course_from_arg(context.args[0] if context.args else None)
            icon, label = ("📘", "lecture") if kind == "lecture" else ("🧪", "lab sheet")
            await update.message.reply_text(f"{icon} Send me the {label} (PDF, PPTX, DOCX, TXT, or photos).")

    async def lecture_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await kind_cmd(update, context, "lecture")

    async def lab_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await kind_cmd(update, context, "lab")

    async def photos_ready(paths: list[Path], meta: dict) -> None:
        await orch.process_photos(paths, meta.get("kind"), meta.get("course"), meta.get("title"))

    def upload_dir() -> Path:
        # One folder per upload, so two different files with the same name never overwrite each other.
        d = orch.settings.data_dir / "uploads" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def kind_course_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A pending /lecture or /lab, overridden by anything in the caption."""
        pending_kind = context.user_data.pop("next_kind", None)  # always consume, so it can't leak
        pending_course = context.user_data.pop("next_course", None)  # into the next file
        kind, course, title = parse_caption(update.message.caption, orch.course_from_arg)
        return kind or pending_kind, course or pending_course, title

    batcher = PhotoBatcher(photos_ready, upload_dir)

    async def document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        doc = update.message.document
        suffix = Path(doc.file_name or "").suffix.lower()
        if suffix in IMAGE_EXT:  # a photo sent as a File (full resolution: best for OCR)
            await image(update, context, doc, suffix)
            return
        if suffix in AUDIO_EXT:  # a recording sent as a File
            await audio_message(update, context, doc, suffix)
            return
        kind, course, _ = kind_course_title(update, context)
        if suffix not in SUPPORTED:
            await update.message.reply_text("I can read PDF, PPTX, DOCX, TXT and MD files, and photos.")
            return
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("Telegram only lets bots download files up to 20 MB. "
                                            "Put it in the course folder instead.")
            return
        dest = upload_dir() / Path(doc.file_name).name
        await (await doc.get_file()).download_to_drive(dest)
        if kind:
            await update.message.reply_text("Got it, working on it… (this can take a minute)")
        # Run in the background so the bot stays responsive while the LLM works.
        context.application.create_task(orch.process_upload(dest, kind, course))

    async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        msg = update.message
        if msg.voice:
            await audio_message(update, context, msg.voice, ".ogg")
        elif msg.video_note:  # round video message: only its audio is used
            await audio_message(update, context, msg.video_note, ".mp4")
        else:
            await audio_message(update, context, msg.audio, Path(msg.audio.file_name or "a.mp3").suffix.lower())

    async def audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE, media, suffix: str) -> None:
        """During a quiz a voice note is the answer; otherwise it's transcribed and summarised like a file."""
        msg = update.message
        if media.file_size and media.file_size > 20 * 1024 * 1024:
            await msg.reply_text("Telegram only lets bots download files up to 20 MB; send a shorter recording.")
            return
        dest = upload_dir() / f"voice_{msg.message_id}{suffix}"
        await (await media.get_file()).download_to_drive(dest)
        explicit = parse_caption(msg.caption, orch.course_from_arg)[0] or context.user_data.get("next_kind")
        if orch.awaiting_quiz_answer() and not explicit:
            await msg.chat.send_action(ChatAction.TYPING)
            await msg.reply_text(await orch.quiz_voice_answer(dest))
            return
        kind, course, title = kind_course_title(update, context)
        context.application.create_task(orch.process_voice(dest, kind, course, title))

    async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await image(update, context, update.message.photo[-1], ".jpg")  # largest size Telegram kept

    async def image(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_obj, suffix: str) -> None:
        msg = update.message
        first = msg.media_group_id is None or msg.media_group_id not in batcher.albums
        # A pending /lecture or /lab belongs to the first photo; later album photos only add their caption.
        kind, course, title = kind_course_title(update, context) if first or msg.caption else (None, None, None)

        async def download(folder: Path) -> Path:
            dest = folder / f"photo_{msg.message_id}{suffix}"
            await (await tg_obj.get_file()).download_to_drive(dest)
            return dest

        await batcher.add(msg.media_group_id, msg.message_id, {"kind": kind, "course": course, "title": title},
                          download)

    async def kind_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not await guard(update):
            await query.answer()
            return
        _, doc_id, kind = query.data.split(":")
        await query.answer()
        if kind not in ("lecture", "lab") or not doc_id.isdigit():
            return
        text = await orch.resolve_kind(int(doc_id), kind)
        await query.edit_message_text(text)

    async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        args = context.args or []
        kinds = {"lectures": "lecture", "lecture": "lecture", "labs": "lab", "lab": "lab"}
        if not args or args[0].lower() not in kinds:
            log.info("Quiz start: usage shown (missing 'lectures' or 'labs')")
            await update.message.reply_text("Usage: /quiz lectures <topic or week>  or  /quiz labs <topic or week>\n"
                                            "e.g. /quiz lectures normalization, /quiz labs week 3, /quiz lectures 205")
            return
        topic = " ".join(args[1:]).strip() or None
        await update.message.reply_text("Preparing questions…")
        await update.message.chat.send_action(ChatAction.TYPING)
        await update.message.reply_text(await orch.quiz_start(kinds[args[0].lower()], topic))

    async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            # Not in a thread: it shares the DB connection. Hashes are cached, so repeat calls are quick.
            text, buttons = orch.files(" ".join(context.args) if context.args else None)
            parts = split_message(text)
            for i, part in enumerate(parts):  # buttons go on the last part
                await update.message.reply_text(part, reply_markup=buttons if i == len(parts) - 1 else None)

    async def summarise_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if not await guard(update):
            return
        action, _, arg = query.data.partition(":")
        if action == "sum" and arg.isdigit():
            text = await orch.summarise_existing(int(arg))
        elif action == "sumall":
            text = await orch.summarise_all(arg)
        else:
            return
        await query.message.reply_text(text)

    async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await update.message.reply_text(orch.weekly_review())

    async def skip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await update.message.reply_text(orch.quiz_skip() or "No quiz running.")

    async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            await update.message.reply_text(orch.quiz_stop())

    async def text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        if orch.quiz is None:
            await update.message.reply_text("I don't know that one yet. Try /help.")
            return
        await update.message.chat.send_action(ChatAction.TYPING)
        reply = await orch.quiz_answer(update.message.text)
        await update.message.reply_text(reply or "No question is waiting for an answer.")

    async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update) and update.message:
            await update.message.reply_text("I don't know that one yet. Try /help.")

    async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Log the shape of every incoming update (never message text or file contents).
        msg = update.effective_message
        if update.callback_query:
            what = "button press"
        elif msg is None:
            what = "non-message update"
        elif msg.document:
            what = f"document {Path(msg.document.file_name or '').suffix or '?'}"
            what += " with caption" if msg.caption else ""
        elif msg.text and msg.text.startswith("/"):
            what = f"command {msg.text.split()[0]}"
        else:
            what = ("photo" if msg.photo else "voice" if msg.voice else "video note" if msg.video_note
                    else "audio" if msg.audio else "text" if msg.text else "other message")
        chat_id = update.effective_chat.id if update.effective_chat else None
        log.info("Incoming %s (allowed chat: %s)", what, is_allowed(chat_id, allowed_chat_id))

    app.add_handler(TypeHandler(Update, log_update), group=-1)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("deadlines", deadlines_cmd))
    app.add_handler(CommandHandler("sync", sync_cmd))
    app.add_handler(CommandHandler("fakedeadline", fake_deadline))
    app.add_handler(CommandHandler("lecture", lecture_cmd))
    app.add_handler(CommandHandler("lab", lab_cmd))
    app.add_handler(CallbackQueryHandler(kind_button, pattern=r"^kind:"))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("skip", skip_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CallbackQueryHandler(summarise_button, pattern=r"^sum(all)?:"))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, document))
    app.add_handler(MessageHandler(filters.PHOTO, photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))
    app.add_handler(MessageHandler(filters.ALL, fallback))
