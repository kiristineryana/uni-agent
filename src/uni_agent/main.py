"""Entry point: `python -m uni_agent` runs the agent; `--check` validates setup and exits."""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import db
from .config import Settings, load_settings

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

    log.info("Nothing to run yet (Phase 0). Exiting cleanly.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
