"""Telegram command and file handlers. Thin: every handler delegates to the orchestrator."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters

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

    async def kind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
        if await guard(update):
            context.user_data["next_kind"] = kind
            context.user_data["next_course"] = orch.course_from_arg(context.args[0] if context.args else None)
            icon, label = ("📘", "lecture") if kind == "lecture" else ("🧪", "lab sheet")
            await update.message.reply_text(f"{icon} Send me the {label} (PDF, PPTX, DOCX or TXT).")

    async def lecture_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await kind_cmd(update, context, "lecture")

    async def lab_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await kind_cmd(update, context, "lab")

    async def document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await guard(update):
            return
        doc = update.message.document
        kind = context.user_data.pop("next_kind", None)
        course = context.user_data.pop("next_course", None)
        caption = (update.message.caption or "").split()
        if caption and caption[0].lower() in ("/lecture", "/lab"):
            kind = caption[0].lower().lstrip("/")
            course = orch.course_from_arg(caption[1] if len(caption) > 1 else None) or course
        if Path(doc.file_name or "").suffix.lower() not in SUPPORTED:
            await update.message.reply_text("I can read PDF, PPTX, DOCX, TXT and MD files.")
            return
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await update.message.reply_text("Telegram only lets bots download files up to 20 MB. "
                                            "Put it in the course folder instead.")
            return
        # One folder per upload, so two different files with the same name never overwrite each other.
        dest_dir = orch.settings.data_dir / "uploads" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(doc.file_name).name
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(dest)
        if kind:
            await update.message.reply_text("Got it, working on it… (this can take a minute)")
        # Run in the background so the bot stays responsive while the LLM works.
        context.application.create_task(orch.process_upload(dest, kind, course))

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
            await update.message.reply_text("Usage: /quiz lectures <topic or week>  or  /quiz labs <topic or week>\n"
                                            "e.g. /quiz lectures normalization, /quiz labs week 3, /quiz lectures 205")
            return
        topic = " ".join(args[1:]).strip() or None
        await update.message.reply_text("Preparing questions…")
        await update.message.chat.send_action(ChatAction.TYPING)
        await update.message.reply_text(await orch.quiz_start(kinds[args[0].lower()], topic))

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
    app.add_handler(CommandHandler("lab", lab_cmd))
    app.add_handler(CallbackQueryHandler(kind_button, pattern=r"^kind:"))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("skip", skip_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))
    app.add_handler(MessageHandler(filters.ALL, fallback))
