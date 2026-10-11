"""Enrolment filter: only the student's courses and sections are alerted; unclear items are flagged, not alerted."""
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from uni_agent import db
from uni_agent.agents import deadlines, enrolment, ingest, weekly
from uni_agent.config import Enrolment, SessionConfig, Settings
from uni_agent.orchestrator import Orchestrator
from uni_agent.sources.moodle_ical import FeedItem

COURSES = {"CSIT205", "CSIT214", "CSCI235"}
SESSIONS = [
    enrolment.Session("CSIT214", "lab", "Avery Bla", "tue", "10:30"),
    enrolment.Session("CSCI235", "lecture", "Blake Mo O", "tue", "12:30"),
    enrolment.Session("CSIT214", "lecture", "Casey Ro", "tue", "14:30"),
    enrolment.Session("CSIT205", "lab", "Dimitrios", "wed", "08:30"),
    enrolment.Session("CSCI235", "lab", "Emery Fo", "wed", "10:30"),
    enrolment.Session("CSIT205", "lecture", "Finley Ra", "wed", "12:30"),
]


def status(title, course, other_staff=()):
    return enrolment.classify(title, course, COURSES, SESSIONS, set(other_staff)).status


@pytest.mark.parametrize("title,course,expected", [
    # real titles from the Moodle feed
    ("Lab 2_Week 3_Robin_Monday 8:30", "CSIT214", "ignored"),
    ("Week 3 Quiz 2 - Emery Foz (Wednesday)", "CSCI235", "open"),
    ("Week 3 Lab 3 Submission - Wednesday Batches (Emery Foz)", "CSCI235", "open"),
    ("Thu 8.30AM Lab 2 Submission", "CSIT214", "ignored"),
    ("Tue 10.30AM Lab 2 Submission", "CSIT214", "open"),  # my lab's day and time, no tutor named
    ("Week 3 Lab 2 Submission Box", "CSIT205", "open"),
    ("Week4_lab4(`Morgan)", "CSCI235", "unclear"),
    ("Week 3 project detail submission", "CSIT205", "open"),
    ("Assignment 1 Submission box - Part 2: Report", "CSIT205", "open"),
    ("Lab 1_Week 2_Avery_Tuesday_10.30AM - Part 1", "CSIT214", "open"),
    ("Lab2_Week 3_Robin_Monday 12:30 PM", "CSIT214", "ignored"),
    ("Week 3 - Lab 2 - In Class Activity (Dimitrios)", "CSIT205", "open"),
    ("Test your Technology - Practice Exam (Remotely Proctored)", "PROJ085", "ignored"),
    # rule 5: never another course, even if it looks like mine
    ("Lab 2_Week 3_Avery_Tuesday_10.30AM", "CSIT999", "ignored"),
    # rule 4: no course anywhere -> flagged, not alerted
    ("Assignment 2 is due", None, "unclear"),
    ("Lab quiz", "General", "unclear"),
    # course code only in the title
    ("CSIT205 Assignment 2", None, "open"),
    # day+time of my session but the other course's slot -> not mine
    ("Wed 12:30 Lab 3", "CSCI235", "ignored"),
    # a due time without a weekday is a deadline, not a group
    ("Assignment 1 due 11:59 PM", "CSCI235", "open"),
    # a bare day word that isn't clearly a group label -> flag rather than guess
    ("Quiz closes Friday", "CSIT214", "unclear"),
    ("Friday Batches Lab 4", "CSIT214", "ignored"),
    ("Tuesday Batches Lab 4", "CSIT214", "unclear"),  # my day, but I have two Tuesday sessions: which?
    # brackets that aren't a person
    ("Assignment 1 (Individual)", "CSIT205", "open"),
    ("Assignment 1 (Part 2)", "CSIT205", "open"),
])
def test_rules_on_titles(title, course, expected):
    assert status(title, course) == expected


def test_other_staff_list_hides_their_groups():
    assert status("Week4_lab4(`Morgan)", "CSCI235", other_staff=["Morgan"]) == "ignored"


def test_my_tutor_wins_even_with_another_day_mentioned():
    assert status("Lab 4 - Emery Foz (Monday makeup)", "CSCI235") == "open"


def test_no_enrolment_means_everything_is_alerted():
    assert enrolment.classify("Lab 2_Week 3_Robin_Monday 8:30", "CSIT214", set(), [], set()).status == "open"


def test_staff_tokens_ignore_truncated_fragments():
    assert enrolment.staff_tokens("Emery Fo") == {"emery"}
    assert enrolment.staff_tokens("Blake Mo O") == {"blake"}


# --- wired through ingest, alerts, /deadlines and the weekly review ---

NOW = db.utcnow().replace(microsecond=0)  # /deadlines reads the real clock


@pytest.fixture
def orch(tmp_path):
    s = Settings(data_dir=tmp_path, voice_enabled=False, ocr_enabled=False, enrolment=Enrolment(
        courses=sorted(COURSES),
        sessions=[SessionConfig(course=x.course, type=x.kind, staff=x.staff, day=x.day, time=x.time) for x in SESSIONS]))
    return Orchestrator(s, db.connect(":memory:"))


def feed(*rows):
    return [FeedItem(f"u{i}", title, course, "lab", NOW + timedelta(hours=h)) for i, (title, course, h) in enumerate(rows)]


def test_only_my_items_are_alerted_and_listed(orch):
    items = feed(("Lab 2_Week 3_Avery_Tuesday_10.30AM", "CSIT214", 2),
                 ("Lab 2_Week 3_Robin_Monday 8:30", "CSIT214", 2),
                 ("Week4_lab4(`Morgan)", "CSCI235", 2),
                 ("Practice Exam", "PROJ085", 2))
    ingest.upsert_items(orch.conn, items, classify=orch._enrolment_classifier())
    alerts = deadlines.pending_alerts(orch.conn, NOW, orch.settings.alert_offsets)
    assert [it.title for it, _ in alerts] == ["Lab 2_Week 3_Avery_Tuesday_10.30AM"]

    text = orch.deadlines()
    assert "Avery" in text and "Robin" not in text.split("⚠️")[0]
    assert "⚠️ Not alerted: unclear" in text and "Morgan" in text
    assert "2 item(s) for other courses/sections hidden: /hidden" in text
    hidden = orch.hidden()
    assert "Robin" in hidden and "PROJ085 is not one of your courses" in hidden

    review = weekly.build_review(orch.conn, NOW, ZoneInfo("Asia/Dubai"))
    assert "Morgan" in review and "Robin" not in review


def test_changing_enrolment_refilters_existing_items(orch):
    items = feed(("Week4_lab4(`Morgan)", "CSCI235", 30))
    ingest.upsert_items(orch.conn, items, classify=orch._enrolment_classifier())
    assert orch.conn.execute("SELECT status FROM items").fetchone()["status"] == "unclear"
    orch.settings.enrolment.other_staff.append("Morgan")
    ingest.upsert_items(orch.conn, items, classify=orch._enrolment_classifier())
    assert orch.conn.execute("SELECT status, note FROM items").fetchone()["status"] == "ignored"


def test_old_database_gets_the_note_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, course TEXT, title TEXT NOT NULL, type TEXT, "
                "due_at TEXT NOT NULL, source TEXT NOT NULL, uid TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'open')")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert "note" in {r["name"] for r in conn.execute("PRAGMA table_info(items)")}


def test_items_dropped_from_the_feed_are_refiltered_too(orch):
    ingest.upsert_items(orch.conn, feed(("Lab 2_Week 3_Robin_Monday 8:30", "CSIT214", 30)))  # stored unfiltered
    assert orch.conn.execute("SELECT status FROM items").fetchone()["status"] == "open"
    assert ingest.reclassify_upcoming(orch.conn, orch._enrolment_classifier(), NOW) == 1
    assert orch.conn.execute("SELECT status FROM items").fetchone()["status"] == "ignored"
