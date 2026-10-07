from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from uni_agent import db
from uni_agent.agents import exam, ingest, weekly
from uni_agent.sources.moodle_ical import FeedItem

DUBAI = ZoneInfo("Asia/Dubai")
NOW = datetime(2026, 10, 11, 14, tzinfo=timezone.utc)  # Sun 11 Oct, 18:00 Dubai


def add_doc(conn, tmp_path, name, kind, course, when):
    p = tmp_path / f"{name}.md"
    p.write_text("summary", encoding="utf-8")
    ingest.record_document(conn, tmp_path / f"{name}.pdf", f"sha-{name}", kind, course, p)
    with conn:
        conn.execute("UPDATE documents SET ingested_at = ? WHERE sha256 = ?", (db.iso(when), f"sha-{name}"))
    return ingest.known_document(conn, f"sha-{name}")["id"]


def test_empty_week(tmp_path):
    text = weekly.build_review(db.connect(":memory:"), NOW, DUBAI)
    assert "📘 Lectures\nCovered: nothing new recorded" in text
    assert "🧪 Labs\nCovered: nothing new recorded" in text
    assert "Nothing on Moodle" in text
    assert "/lecture" in text  # nudges to add material


def test_full_week_has_separate_sections_due_items_and_three_tasks(tmp_path):
    conn = db.connect(":memory:")
    lec = add_doc(conn, tmp_path, "week2lecture2_Normalization", "lecture", "CSCI235", NOW - timedelta(days=2))
    add_doc(conn, tmp_path, "CSIT205_L02", "lecture", "CSIT205", NOW - timedelta(days=1))
    lab = add_doc(conn, tmp_path, "Week_3_Lab_2", "lab", "CSIT205", NOW - timedelta(days=3))
    add_doc(conn, tmp_path, "Old_lecture", "lecture", "CSIT214", NOW - timedelta(days=12))  # last week

    exam.store_questions(conn, lec, "lecture", [{"topic": "3NF", "question": "q1", "answer": "a"},
                                               {"topic": "Keys", "question": "q2", "answer": "a"}])
    exam.store_questions(conn, lab, "lab", [{"topic": "Cursors", "question": "q3", "answer": "a"}])
    q = {r["question"]: r["id"] for r in conn.execute("SELECT id, question FROM quiz_questions")}
    exam.record_answer(conn, q["q1"], "wrong", NOW - timedelta(days=1))
    exam.record_answer(conn, q["q2"], "correct", NOW - timedelta(days=1))
    exam.record_answer(conn, q["q3"], "partly", NOW - timedelta(days=1))

    ingest.upsert_items(conn, [
        FeedItem("u1", "Week 4 Lab 3 Submission Box", "CSIT205", "lab", NOW + timedelta(days=2)),
        FeedItem("u2", "Assignment 1", "CSCI235", "assignment", NOW + timedelta(days=5)),
        FeedItem("u3", "Final exam", "CSIT214", "exam", NOW + timedelta(days=20)),  # beyond 7 days
    ])

    text = weekly.build_review(conn, NOW, DUBAI)
    lectures, labs = text.split("📘 Lectures")[1].split("🧪 Labs")
    assert "week2lecture2 Normalization (CSCI235)" in lectures and "CSIT205 L02" in lectures
    assert "Old lecture" not in text
    assert "Week 3 Lab 2" in labs.split("⏰")[0] and "Normalization" not in labs.split("⏰")[0]
    assert "Weakest topics: 3NF (0%)" in lectures
    assert "Weakest topics: Cursors (50%)" in labs
    assert "Quizzes: 2 answers, 50% average" in lectures

    due = text.split("⏰ Due in the next 7 days")[1].split("📝")[0]
    assert "Week 4 Lab 3" in due and "Assignment 1" in due and "Final exam" not in due

    tasks = text.split("📝 Suggested revision\n")[1].splitlines()
    assert len(tasks) == 3
    assert tasks[0].startswith('1. Revise "3NF" (0%') and "/quiz lectures 3NF" in tasks[0]
    assert "/quiz labs Cursors" in tasks[1]
    assert "Week 4 Lab 3" in tasks[2] and "/quiz labs CSIT205" in tasks[2]


def test_unquizzed_material_suggested_when_no_weak_topics(tmp_path):
    conn = db.connect(":memory:")
    add_doc(conn, tmp_path, "Lecture 3 - Scope", "lecture", "CSIT214", NOW - timedelta(days=1))
    tasks = weekly.build_review(conn, NOW, DUBAI).split("📝 Suggested revision\n")[1]
    assert "/quiz lectures Lecture 3 - Scope" in tasks


def test_lab_due_without_briefing_suggests_sending_the_sheet(tmp_path):
    conn = db.connect(":memory:")
    ingest.upsert_items(conn, [FeedItem("u1", "Week 4 Lab 3 Submission Box", "CSIT205", "lab", NOW + timedelta(days=2))])
    tasks = weekly.build_review(conn, NOW, DUBAI).split("📝 Suggested revision\n")[1]
    assert "/lab CSIT205" in tasks and "/quiz labs" not in tasks


def test_suggested_weak_topic_quiz_actually_finds_questions(tmp_path):
    conn = db.connect(":memory:")
    doc = add_doc(conn, tmp_path, "Lecture 8-Multimedia Applications", "lecture", "Uploads", NOW - timedelta(days=1))
    exam.store_questions(conn, doc, "lecture", [{"topic": "CDN Deployment Strategies", "question": "q", "answer": "a"}])
    qid = conn.execute("SELECT id FROM quiz_questions").fetchone()["id"]
    exam.record_answer(conn, qid, "wrong", NOW - timedelta(hours=1))
    text = weekly.build_review(conn, NOW, DUBAI)
    assert "/quiz lectures CDN Deployment Strategies" in text
    assert [r["id"] for r in exam.matching_docs(conn, "lecture", "CDN Deployment Strategies")] == [doc]
