"""Weekly Review agent: what was covered (lectures vs labs), weak quiz topics, what's due, what to revise.
Rule-based on purpose: costs no LLM requests and is predictable. No Telegram."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import db
from . import deadlines, exam, ingest


@dataclass
class KindWeek:
    covered: list[tuple[str, str]] = field(default_factory=list)  # (course, title)
    answered: int = 0
    avg: float | None = None
    weak: list[tuple[str, float, int]] = field(default_factory=list)
    unquizzed: list[tuple[str, str]] = field(default_factory=list)


def _kind_week(conn: sqlite3.Connection, kind: str, since: datetime) -> KindWeek:
    kw = KindWeek()
    rows = conn.execute(
        "SELECT id, course, path FROM documents WHERE kind = ? AND status = 'done' AND ingested_at >= ? "
        "ORDER BY ingested_at", (kind, db.iso(since))).fetchall()
    for r in rows:
        item = (r["course"], ingest.title_from_path(Path(r["path"])))
        kw.covered.append(item)
        if not exam.question_count(conn, r["id"]):
            kw.unquizzed.append(item)
    stats = conn.execute(
        "SELECT COUNT(*) n, AVG(l.correct) avg FROM quiz_log l JOIN quiz_questions q ON q.id = l.question_id "
        "WHERE q.kind = ? AND l.answered_at >= ?", (kind, db.iso(since))).fetchone()
    kw.answered, kw.avg = stats["n"], stats["avg"]
    kw.weak = exam.weak_topics(conn, kind, since)
    return kw


def suggestions(lec: KindWeek, lab: KindWeek, due: list[deadlines.Item], now: datetime,
                tz: ZoneInfo, n: int = 3, lab_courses: set[str] = frozenset()) -> list[str]:
    """Priority: weakest topics, then labs due soon, then this week's material not yet quizzed.
    `lab_courses`: courses that have a lab briefing (so /quiz labs <course> can actually run)."""
    out: list[str] = []
    weak = sorted([(t, a, "lectures") for t, a, _ in lec.weak] + [(t, a, "labs") for t, a, _ in lab.weak],
                  key=lambda x: x[1])
    for topic, avg, which in weak[:2]:
        out.append(f"Revise \"{topic}\" ({round(avg * 100)}% in quizzes): /quiz {which} {topic}")
    for item in due:
        if len(out) >= n:
            break
        if deadlines.kind_tag(item) == "🧪 ":
            how = (f"re-read the briefing, then /quiz labs {item.course}" if item.course in lab_courses
                   else f"send the lab sheet with /lab {item.course} for a briefing")
            out.append(f"Prepare {item.title} ({item.course}), due {deadlines.fmt_due(item.due_at, tz)} "
                       f"(in {deadlines.countdown(item.due_at - now)}): {how}")
    for course, title in lec.unquizzed + lab.unquizzed:
        if len(out) >= n:
            break
        which = "lectures" if (course, title) in lec.unquizzed else "labs"
        out.append(f"Quiz yourself on {title} ({course}): /quiz {which} {title}")
    if len(out) < n and not (lec.covered or lab.covered):
        out.append("No new material this week: send this week's slides with /lecture and lab sheets with /lab.")
    return out[:n]


def _section(icon: str, name: str, kw: KindWeek) -> str:
    lines = [f"{icon} {name}"]
    if kw.covered:
        lines.append("Covered: " + "; ".join(f"{t} ({c})" for c, t in kw.covered))
    else:
        lines.append("Covered: nothing new recorded")
    if kw.answered:
        plural = "answer" if kw.answered == 1 else "answers"
        lines.append(f"Quizzes: {kw.answered} {plural}, {round((kw.avg or 0) * 100)}% average")
    lines.append("Weakest topics: " + (", ".join(f"{t} ({round(a * 100)}%)" for t, a, _ in kw.weak)
                                        if kw.weak else ("none, nice!" if kw.answered else "no quiz data yet")))
    return "\n".join(lines)


def build_review(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo) -> str:
    since = now - timedelta(days=7)
    lec, lab = _kind_week(conn, "lecture", since), _kind_week(conn, "lab", since)
    due = deadlines.upcoming(conn, now, timedelta(days=7))

    parts = [f"🗓️ Weekly review · week ending {now.astimezone(tz).strftime('%a %d %b')}",
             _section("📘", "Lectures", lec),
             _section("🧪", "Labs", lab)]
    if due:
        parts.append("⏰ Due in the next 7 days\n" + "\n".join(
            f"• {deadlines.kind_tag(i)}[{i.course}] {i.title} · {deadlines.fmt_due(i.due_at, tz)} "
            f"(in {deadlines.countdown(i.due_at - now)})" for i in due))
    else:
        parts.append("⏰ Due in the next 7 days\nNothing on Moodle. 🎉")
    unclear = deadlines.filtered(conn, now, timedelta(days=7), "unclear")
    if unclear:
        parts.append(deadlines.unclear_text(unclear, now, tz))
    lab_courses = {r["course"] for r in conn.execute(
        "SELECT DISTINCT course FROM documents WHERE kind = 'lab' AND status = 'done'")}
    tasks = suggestions(lec, lab, due, now, tz, lab_courses=lab_courses)
    if tasks:
        parts.append("📝 Suggested revision\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(tasks, 1)))
    return "\n\n".join(parts)
