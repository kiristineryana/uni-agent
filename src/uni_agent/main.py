"""Entry point: `python -m uni_agent` runs the agent; `--check` validates setup and exits."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, Update
from telegram.ext import Application

from . import bot, db, notify
from .config import Settings, load_settings
from .llm import make_llm
from .orchestrator import Orchestrator
from .sources.folder_watch import FolderWatcher

log = logging.getLogger("uni_agent")


def setup_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        settings.log_dir / "uni_agent.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [file_handler, console]
    # httpx logs full request URLs, which contain the bot token and iCal token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def check(settings: Settings) -> list[str]:
    """Return a list of human-readable setup problems (empty = all good)."""
    problems = []
    s = settings.secrets
    if not s.telegram_bot_token:
        problems.append("TELEGRAM_BOT_TOKEN is not set in .env")
    if not s.telegram_chat_id:
        problems.append("TELEGRAM_CHAT_ID is not set in .env")
    if not s.llm_api_key:
        problems.append("LLM_API_KEY is not set in .env")
    if not s.moodle_ical_url:
        problems.append("MOODLE_ICAL_URL is not set in .env (deadlines disabled)")
    for folder in settings.course_folders:
        if not folder.is_dir():
            problems.append(f"Course folder does not exist: {folder}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uni-agent")
    parser.add_argument("--check", action="store_true", help="validate setup and exit")
    parser.add_argument("--test-alert", action="store_true", help="send one test message and exit")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings)
    conn = db.connect(settings.db_path)

    problems = check(settings)
    for p in problems:
        log.warning(p)
    log.info("DB ready at %s; watching %d course folders", settings.db_path, len(settings.course_folders))

    if args.check:
        log.info("Check finished with %d warning(s).", len(problems))
        conn.close()
        return 0

    s = settings.secrets
    if not (s.telegram_bot_token and s.telegram_chat_id):
        log.error("Telegram is not configured; fill TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return 1

    if args.test_alert:
        asyncio.run(_send_once(settings, "⏰ Test alert from Uni Agent. If you see this, alerts work."))
        log.info("Test alert sent.")
        return 0

    run_bot(settings, conn)
    conn.close()
    return 0


async def _send_once(settings: Settings, text: str) -> None:
    tg_bot = Bot(settings.secrets.telegram_bot_token.get_secret_value())
    async with tg_bot:
        await notify.Notifier(tg_bot, settings.secrets.telegram_chat_id).send(text)


def run_bot(settings: Settings, conn) -> None:
    orch = Orchestrator(settings, conn, make_llm(settings, conn))
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    watcher = FolderWatcher(settings.course_folders, settings.ignore_patterns, orch.on_new_file)

    async def post_init(app: Application) -> None:
        notify.configure(notify.Notifier(app.bot, settings.secrets.telegram_chat_id))
        db.kv_set(conn, "started_at", db.iso(db.utcnow()))
        await asyncio.to_thread(orch.baseline_folders)
        watcher.start()
        sch = settings.schedules
        now = datetime.now(settings.tz)
        # Ingest (Moodle + folder rescan) first, then check alerts shortly after.
        scheduler.add_job(orch.run_ingest, "interval", minutes=sch.ingest_every_minutes,
                          next_run_time=now, id="ingest", max_instances=1, coalesce=True)
        scheduler.add_job(orch.run_deadline_check, "interval", minutes=sch.deadline_check_every_minutes,
                          next_run_time=now + timedelta(seconds=30), id="deadline_check",
                          max_instances=1, coalesce=True)
        scheduler.start()
        log.info("Bot started; long polling.")
        await notify.send_telegram("🟢 Uni Agent is online. /help for commands.")

    async def post_shutdown(app: Application) -> None:
        watcher.stop()
        if scheduler.running:
            scheduler.shutdown(wait=False)
        log.info("Bot stopped.")

    app = (
        Application.builder()
        .token(settings.secrets.telegram_bot_token.get_secret_value())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    bot.register(app, orch, settings.secrets.telegram_chat_id)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    sys.exit(main())
