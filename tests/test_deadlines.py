from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from uni_agent import db
from uni_agent.agents import deadlines, ingest
from uni_agent.sources import moodle_ical
from uni_agent.sources.moodle_ical import FeedItem

DUBAI = ZoneInfo("Asia/Dubai")
OFFSETS = {"7d": 10080, "3d": 4320, "1d": 1440, "3h": 180}
UTC = timezone.utc

SAMPLE_ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Moodle Pty Ltd//NONSGML Moodle Version 2024//EN
BEGIN:VEVENT
UID:101@moodle.example
SUMMARY:Assignment 2 is due
DTSTART:20261017T195900Z
CATEGORIES:CSIT205
END:VEVENT
BEGIN:VEVENT
UID:102@moodle.example
SUMMARY:Quiz 1 opens
DTSTART:20261010T050000Z
CATEGORIES:CSCI235
END:VEVENT
BEGIN:VEVENT
UID:103@moodle.example
SUMMARY:Quiz 1 closes
DTSTART:20261012T050000Z
CATEGORIES:CSCI235
END:VEVENT
BEGIN:VEVENT
UID:104@moodle.example
SUMMARY:Lab 4 submission
DTSTART;VALUE=DATE:20261020
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def conn():
    return db.connect(":memory:")


def add(conn, due, title="Assignment 2", uid="u1"):
    ingest.upsert_items(conn, [FeedItem(uid, title, "CSIT205", "assignment", due)])
    return conn.execute("SELECT id FROM items WHERE uid = ?", (uid,)).fetchone()["id"]


# --- iCal parsing ---

def test_parse_moodle_feed():
    items = {i.uid: i for i in moodle_ical.parse(SAMPLE_ICS, DUBAI)}
    assert "102@moodle.example" not in items  # "opens" is skipped
    a2 = items["101@moodle.example"]
    assert (a2.title, a2.course, a2.type) == ("Assignment 2", "CSIT205", "assignment")
    assert a2.due_at == datetime(2026, 10, 17, 19, 59, tzinfo=UTC)
    assert items["103@moodle.example"].title == "Quiz 1" and items["103@moodle.example"].type == "quiz"
    # all-day date -> 23:59 Dubai time
    lab = items["104@moodle.example"]
    assert lab.type == "lab" and lab.course is None
    assert lab.due_at.astimezone(DUBAI) == datetime(2026, 10, 20, 23, 59, tzinfo=DUBAI)


@pytest.mark.parametrize("raw,clean", [("CSIT205_DB426", "CSIT205"), ("PROJ085_21", "PROJ085"),
                                       ("CSCI235", "CSCI235"), ("General", "General")])
def test_clean_course(raw, clean):
    assert moodle_ical.clean_course(raw) == clean


def test_ignore_patterns_drop_other_sections():
    items = moodle_ical.parse(SAMPLE_ICS, DUBAI, ignore=["QUIZ 1"])
    assert {i.uid for i in items} == {"101@moodle.example", "104@moodle.example"}


# --- offsets / dedupe ---

def test_no_alert_before_first_threshold(conn):
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    add(conn, now + timedelta(days=8))
    assert deadlines.pending_alerts(conn, now, OFFSETS) == []


def test_each_threshold_fires_once(conn):
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    item_id = add(conn, now + timedelta(days=10))
    fired = []
    t = now
    while t < now + timedelta(days=10):  # simulate the 15-minute job
        for item, labels in deadlines.pending_alerts(conn, t, OFFSETS):
            fired.append(labels[0])
            deadlines.mark_sent(conn, item.id, labels, t)
        t += timedelta(minutes=15)
    assert fired == ["7d", "3d", "1d", "3h"]
    rows = conn.execute("SELECT COUNT(*) n FROM alerts_sent WHERE item_id = ?", (item_id,)).fetchone()
    assert rows["n"] == 4


def test_late_discovery_sends_only_closest_label(conn):
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    item_id = add(conn, now + timedelta(days=2))
    [(item, labels)] = deadlines.pending_alerts(conn, now, OFFSETS)
    assert labels[0] == "3d" and set(labels) == {"7d", "3d"}
    deadlines.mark_sent(conn, item_id, labels, now)
    assert deadlines.pending_alerts(conn, now + timedelta(minutes=15), OFFSETS) == []


def test_mark_sent_is_idempotent(conn):
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    item_id = add(conn, now + timedelta(hours=1))
    deadlines.mark_sent(conn, item_id, ["3h"], now)
    deadlines.mark_sent(conn, item_id, ["3h"], now)
    assert conn.execute("SELECT COUNT(*) n FROM alerts_sent").fetchone()["n"] == 1


def test_past_items_never_alert(conn):
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    add(conn, now - timedelta(minutes=1))
    assert deadlines.pending_alerts(conn, now, OFFSETS) == []


def test_moved_deadline_resets_alerts(conn):
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    item_id = add(conn, now + timedelta(hours=2))
    deadlines.mark_sent(conn, item_id, ["7d", "3d", "1d", "3h"], now)
    add(conn, now + timedelta(days=5))  # extension: same uid, later due
    [(_, labels)] = deadlines.pending_alerts(conn, now, OFFSETS)
    assert labels == ["7d"]


def test_offset_is_absolute_across_timezones(conn):
    # 23:59 Dubai == 19:59 UTC; the 3h alert window opens at 20:59 Dubai == 16:59 UTC.
    due = datetime(2026, 10, 17, 23, 59, tzinfo=DUBAI)
    add(conn, due)
    just_before = datetime(2026, 10, 17, 16, 58, tzinfo=UTC)
    at = datetime(2026, 10, 17, 20, 59, tzinfo=DUBAI)
    labels_before = [l for _, ls in deadlines.pending_alerts(conn, just_before, OFFSETS) for l in ls]
    labels_at = [l for _, ls in deadlines.pending_alerts(conn, at, OFFSETS) for l in ls]
    assert "3h" not in labels_before and labels_at[0] == "3h"


# --- formatting ---

def test_alert_text_uses_local_time():
    now = datetime(2026, 10, 14, 19, 59, tzinfo=UTC)
    item = deadlines.Item(1, "CSIT205", "Assignment 2", "assignment", datetime(2026, 10, 17, 19, 59, tzinfo=UTC))
    assert deadlines.alert_text(item, now, DUBAI) == "⏰ [CSIT205] Assignment 2 is due in 3 days (Sat 17 Oct, 23:59)"


def test_lab_items_are_labelled():
    item = deadlines.Item(1, "CSCI235", "Lab 4 submission", "lab", datetime(2026, 10, 17, tzinfo=UTC))
    assert "🧪" in deadlines.alert_text(item, datetime(2026, 10, 16, tzinfo=UTC), DUBAI)


@pytest.mark.parametrize("minutes,text", [(1, "1 minute"), (59, "59 minutes"), (60, "1 hour"),
                                          (170, "3 hours"), (23 * 60, "1 day"), (2 * 1440 + 1400, "3 days")])
def test_countdown(minutes, text):
    assert deadlines.countdown(timedelta(minutes=minutes)) == text
