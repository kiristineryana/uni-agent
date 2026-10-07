"""Read-only Moodle calendar export (iCal) fetch + parse. Never logs the URL (it contains a token)."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar

log = logging.getLogger(__name__)


class FeedError(Exception):
    """Fetch failed. The message never contains the URL."""


@dataclass
class FeedItem:
    uid: str
    title: str
    course: str | None
    type: str
    due_at: datetime  # aware, UTC


_COURSE_CODE = re.compile(r"[A-Z]{3,4}\d{3}")


def clean_course(category: str) -> str:
    """'CSIT205_DB426' -> 'CSIT205'; anything without a course code is kept as-is."""
    m = _COURSE_CODE.match(category.strip())
    return m.group(0) if m else category.strip()


_SUFFIXES = re.compile(r"\s+(is due|closes|should be completed|due)\s*$", re.IGNORECASE)


def _classify(summary: str) -> str:
    s = summary.lower()
    if "exam" in s:
        return "exam"
    if "quiz" in s:
        return "quiz"
    if "lab" in s:
        return "lab"
    if "is due" in s or "assignment" in s or "submission" in s:
        return "assignment"
    return "event"


def _to_utc(value, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):  # all-day event: treat as end of that day locally
        return datetime.combine(value, time(23, 59), tz).astimezone(timezone.utc)
    raise ValueError(f"unsupported date value: {value!r}")


def parse(ics: bytes | str, tz: ZoneInfo, ignore: list[str] = ()) -> list[FeedItem]:
    """`ignore`: case-insensitive substrings; matching events (e.g. other lab sections) are dropped."""
    cal = Calendar.from_ical(ics)
    ignore_lc = [p.lower() for p in ignore]
    items: list[FeedItem] = []
    for ev in cal.walk("VEVENT"):
        summary = str(ev.get("SUMMARY", "")).strip()
        uid = str(ev.get("UID", "")).strip()
        start = ev.get("DTSTART")
        if not summary or not uid or start is None:
            continue
        if summary.lower().endswith(" opens"):
            continue  # "Quiz 1 opens" is not a deadline
        if any(p in summary.lower() for p in ignore_lc):
            continue
        cats = ev.get("CATEGORIES")
        course = None
        if cats is not None:
            cat_list = cats if isinstance(cats, list) else [cats]
            names = [str(c) for cl in cat_list for c in getattr(cl, "cats", [cl])]
            course = clean_course(names[0]) if names else None
        items.append(
            FeedItem(
                uid=uid,
                title=_SUFFIXES.sub("", summary),
                course=course,
                type=_classify(summary),
                due_at=_to_utc(start.dt, tz),
            )
        )
    return items


async def fetch(url: str, attempts: int = 3, timeout: float = 20.0) -> bytes:
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url)
            if resp.status_code == 200 and b"BEGIN:VCALENDAR" in resp.content[:2000]:
                return resp.content
            reason = f"HTTP {resp.status_code}" if resp.status_code != 200 else "response is not an iCal feed"
        except httpx.HTTPError as e:
            reason = type(e).__name__  # str(e) may include the URL
        log.warning("Moodle feed fetch failed (%s), attempt %d/%d", reason, attempt, attempts)
        if attempt < attempts:
            await asyncio.sleep(delay)
            delay *= 2
    raise FeedError(f"Moodle feed unavailable ({reason})")
