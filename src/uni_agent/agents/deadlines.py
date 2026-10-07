"""Deadline agent: decides which alerts are due and formats deadline text. Pure; no Telegram."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db


@dataclass
class Item:
    id: int
    course: str | None
    title: str
    type: str | None
    due_at: datetime  # aware UTC


def _row_to_item(r: sqlite3.Row) -> Item:
    return Item(r["id"], r["course"], r["title"], r["type"], datetime.fromisoformat(r["due_at"]))


def upcoming(conn: sqlite3.Connection, now: datetime, within: timedelta) -> list[Item]:
    rows = conn.execute(
        "SELECT * FROM items WHERE status = 'open' AND due_at > ? AND due_at <= ? ORDER BY due_at",
        (db.iso(now), db.iso(now + within)),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def pending_alerts(conn: sqlite3.Connection, now: datetime, offsets: dict[str, int]) -> list[tuple[Item, list[str]]]:
    """For each upcoming item, return the offset labels whose time has passed and weren't sent.
    Only the closest label is announced; the rest are marked sent silently (no 7d alert 2 days out)."""
    horizon = timedelta(minutes=max(offsets.values()))
    result = []
    for item in upcoming(conn, now, horizon):
        sent = {r["offset_label"] for r in conn.execute(
            "SELECT offset_label FROM alerts_sent WHERE item_id = ?", (item.id,))}
        due_labels = [label for label, mins in offsets.items()
                      if now >= item.due_at - timedelta(minutes=mins) and label not in sent]
        if due_labels:
            result.append((item, sorted(due_labels, key=lambda l: offsets[l])))  # closest first
    return result


def mark_sent(conn: sqlite3.Connection, item_id: int, labels: list[str], now: datetime) -> None:
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO alerts_sent(item_id, offset_label, sent_at) VALUES (?, ?, ?)",
            [(item_id, label, db.iso(now)) for label in labels],
        )


def countdown(delta: timedelta) -> str:
    mins = max(int(delta.total_seconds() // 60), 0)
    if mins >= 23 * 60:
        n = round(mins / 1440)
        return f"{n} day{'s' if n != 1 else ''}"
    if mins >= 60:
        n = round(mins / 60)
        return f"{n} hour{'s' if n != 1 else ''}"
    return f"{mins} minute{'s' if mins != 1 else ''}"


def kind_tag(item: Item) -> str:
    text = f"{item.type or ''} {item.title}".lower()
    if "lab" in text or "tutorial" in text:
        return "🧪 "
    if "lecture" in text:
        return "📘 "
    return ""


def fmt_due(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%a %d %b, %H:%M")


def alert_text(item: Item, now: datetime, tz: ZoneInfo) -> str:
    course = f"[{item.course}] " if item.course else ""
    return (f"⏰ {kind_tag(item)}{course}{item.title} is due in {countdown(item.due_at - now)} "
            f"({fmt_due(item.due_at, tz)})")


def deadlines_text(items: list[Item], now: datetime, tz: ZoneInfo, days: int) -> str:
    if not items:
        return f"🎉 Nothing due in the next {days} days."
    lines = [f"📅 Due in the next {days} days"]
    for it in items:
        course = f"[{it.course}] " if it.course else ""
        lines.append(f"• {kind_tag(it)}{course}{it.title}\n   {fmt_due(it.due_at, tz)} · in {countdown(it.due_at - now)}")
    return "\n".join(lines)
