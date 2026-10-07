"""Telegram command and file handlers. Thin: every handler delegates to the orchestrator."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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

    async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guard(update) and update.message:
            await update.message.reply_text("I don't know that one yet. Try /help.")

    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("deadlines", deadlines_cmd))
    app.add_handler(CommandHandler("sync", sync_cmd))
    app.add_handler(CommandHandler("fakedeadline", fake_deadline))
    app.add_handler(MessageHandler(filters.ALL, fallback))
