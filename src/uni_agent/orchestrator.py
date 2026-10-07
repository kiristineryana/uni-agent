"""Routes commands and events to sub-agents. The only place that knows which agent does what."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import db
from .config import Settings

HELP_TEXT = """🎓 Uni Agent

/deadlines – what's due in the next 14 days
/lecture – then send a file: lecture summary
/lab – then send a file: lab briefing
/quiz lectures <topic> – theory quiz
/quiz labs <topic> – practical/viva quiz
/stop – end a quiz
/week – weekly review now
/status – sync time, docs, next alerts, LLM usage
/help – this message

Send a file with no command and I'll ask whether it's a lecture or a lab."""


def is_allowed(chat_id: int | None, allowed_chat_id: int | None) -> bool:
    return chat_id is not None and allowed_chat_id is not None and chat_id == allowed_chat_id


class Orchestrator:
    def __init__(self, settings: Settings, conn: sqlite3.Connection):
        self.settings = settings
        self.conn = conn

    def help(self) -> str:
        return HELP_TEXT

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
            f"({self.settings.llm.provider})"
        )
