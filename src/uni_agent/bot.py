"""Telegram command and file handlers. Thin: every handler delegates to the orchestrator."""
from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters

from .extract import SUPPORTED
from .orchestrator import Orchestrator, is_allowed

log = logging.getLogger(__name__)


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

    async def fake_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            minutes = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
            await update.message.reply_text(await orch.add_fake_deadline(minutes))

    async def lecture_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update):
            context.user_data["next_kind"] = "lecture"
            await update.message.reply_text("📘 Send me the lecture file (PDF, PPTX, DOCX or TXT).")

    async def document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        doc = update.message.document
        kind = context.user_data.pop("next_kind", None)
        caption = (update.message.caption or "").strip().lower()
        if caption.startswith("/lecture"):
            kind = "lecture"
        if kind is None:
            await update.message.reply_text("Send /lecture first, then the file "
                                            "(or put /lecture in the file's caption).")
            return
        if Path(doc.file_name or "").suffix.lower() not in SUPPORTED:
            await update.message.reply_text("I can read PDF, PPTX, DOCX, TXT and MD files.")
            return
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("Telegram only lets bots download files up to 20 MB. "
                                            "Put it in the course folder instead.")
            return
        uploads = orch.settings.data_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        dest = uploads / Path(doc.file_name).name
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(dest)
        await update.message.reply_text("Got it, summarising… (this can take a minute)")
        # Run in the background so the bot stays responsive while the LLM works.
        context.application.create_task(orch.process_upload(dest, kind))

    async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update) and update.message:
            await update.message.reply_text("I don't know that one yet. Try /help.")

    async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Log the shape of every incoming update (never message text or file contents).
        msg = update.effective_message
        if msg is None:
            what = "non-message update"
        elif msg.document:
            what = f"document {Path(msg.document.file_name or '').suffix or '?'}"
            what += " with caption" if msg.caption else ""
        elif msg.text and msg.text.startswith("/"):
            what = f"command {msg.text.split()[0]}"
        else:
            what = "photo" if msg.photo else "voice" if msg.voice else "text" if msg.text else "other message"
        chat_id = update.effective_chat.id if update.effective_chat else None
        log.info("Incoming %s (allowed chat: %s)", what, is_allowed(chat_id, allowed_chat_id))

    app.add_handler(TypeHandler(Update, log_update), group=-1)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("deadlines", deadlines_cmd))
    app.add_handler(CommandHandler("sync", sync_cmd))
    app.add_handler(CommandHandler("fakedeadline", fake_deadline))
    app.add_handler(CommandHandler("lecture", lecture_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, document))
    app.add_handler(MessageHandler(filters.ALL, fallback))
