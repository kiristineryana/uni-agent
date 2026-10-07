import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from uni_agent import db
from uni_agent.agents import exam, ingest
from uni_agent.config import Settings
from uni_agent.llm import LLM, LLMError, LLMResult
from uni_agent.orchestrator import Orchestrator

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


class ScriptedProvider:
    """Returns queued replies in order and records (system, prompt)."""
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, prompt, max_tokens):
        self.calls.append((system, prompt))
        return LLMResult(self.replies.pop(0), 10, 10)


def llm_with(conn, replies):
    return LLM(ScriptedProvider(replies), conn, 10**9, Settings().tz)


QS = json.dumps([
    {"topic": "Normalization", "question": "What is 1NF?", "answer": "Atomic values, no repeating groups."},
    {"topic": "Keys", "question": "Define a candidate key.", "answer": "A minimal superkey."},
])


# --- Leitner ---

@pytest.mark.parametrize("box,verdict,expected", [
    (1, "correct", 2), (4, "correct", 5), (5, "correct", 5),
    (3, "wrong", 1), (1, "wrong", 1), (3, "partly", 3),
])
def test_next_box(box, verdict, expected):
    assert exam.next_box(box, verdict) == expected


def test_next_due_grows_with_box():
    assert exam.next_due(1, NOW) == NOW + timedelta(days=1)
    assert exam.next_due(5, NOW) == NOW + timedelta(days=16)


# --- JSON from the model ---

def test_parse_json_tolerates_fences_and_prose():
    assert exam._parse_json('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert exam._parse_json('Sure! Here you go: {"verdict": "correct"} hope it helps') == {"verdict": "correct"}
    with pytest.raises(LLMError):
        exam._parse_json("no json here")


def test_generate_questions_drops_incomplete_items_and_uses_lab_template():
    conn = db.connect(":memory:")
    bad = json.dumps([{"question": "Q only"}, {"topic": "T", "question": "Why X?", "answer": "Because Y."}])
    llm = llm_with(conn, [bad])
    qs = exam.generate_questions(llm, "summary", "lab", "Lab 2")
    assert qs == [{"topic": "T", "question": "Why X?", "answer": "Because Y."}]
    assert "VIVA" in llm.provider.calls[0][1]


def test_mark_answer_falls_back_on_unknown_verdict():
    conn = db.connect(":memory:")
    llm = llm_with(conn, ['{"verdict": "excellent", "explanation": "x"}'])
    assert exam.mark_answer(llm, "Q", "A", "my answer")[0] == "partly"


# --- storage ---

@pytest.fixture
def setup(tmp_path):
    conn = db.connect(":memory:")

    def add_doc(name, kind, course, summary):
        p = tmp_path / f"{name}.md"
        p.write_text(summary + "\n\n---\nSource: x\n", encoding="utf-8")
        ingest.record_document(conn, tmp_path / f"{name}.pdf", f"sha-{name}", kind, course, p)
        return ingest.known_document(conn, f"sha-{name}")["id"]

    return conn, add_doc


def test_matching_docs_by_week_course_and_content(setup):
    conn, add = setup
    add("Week_3_Lab_2", "lab", "CSIT205", "PL/SQL loops")
    add("week2lecture2_Normalization", "lecture", "CSCI235", "Functional dependencies and 3NF")
    add("Lecture 1 - Intro to PM", "lecture", "CSIT214", "Project charters")
    titles = lambda rows: [Path(r["path"]).stem for r in rows]
    assert titles(exam.matching_docs(conn, "lab", "week 3")) == ["Week_3_Lab_2"]
    assert titles(exam.matching_docs(conn, "lecture", "235")) == ["week2lecture2_Normalization"]
    assert titles(exam.matching_docs(conn, "lecture", "3NF")) == ["week2lecture2_Normalization"]
    assert exam.matching_docs(conn, "lab", "normalization") == []  # kind filter: labs never get lecture docs
    assert len(exam.matching_docs(conn, "lecture", None)) == 2


def test_read_summary_strips_source_footer(setup):
    conn, add = setup
    add("L1", "lecture", "C", "Body text")
    row = exam.matching_docs(conn, "lecture", None)[0]
    assert exam.read_summary(row["summary_path"]) == "Body text"


def test_pick_prefers_due_and_low_boxes(setup):
    conn, add = setup
    doc = add("L1", "lecture", "C", "s")
    exam.store_questions(conn, doc, "lecture", [{"topic": "t", "question": f"q{i}", "answer": "a"} for i in range(3)])
    ids = [r["id"] for r in conn.execute("SELECT id FROM quiz_questions ORDER BY id")]
    exam.record_answer(conn, ids[0], "correct", NOW)  # box 2, due tomorrow
    picked = exam.pick_questions(conn, [doc], NOW, limit=3)
    assert picked[-1] == ids[0] and set(picked) == set(ids)
    assert exam.pick_questions(conn, [doc], NOW + timedelta(days=3), limit=3)[0] in ids


def test_record_answer_moves_boxes_and_logs(setup):
    conn, add = setup
    doc = add("L1", "lecture", "C", "s")
    exam.store_questions(conn, doc, "lecture", [{"topic": "t", "question": "q", "answer": "a"}])
    qid = conn.execute("SELECT id FROM quiz_questions").fetchone()["id"]
    assert exam.record_answer(conn, qid, "correct", NOW) == 2
    assert exam.record_answer(conn, qid, "correct", NOW) == 3
    assert exam.record_answer(conn, qid, "wrong", NOW) == 1
    assert [r["correct"] for r in conn.execute("SELECT correct FROM quiz_log ORDER BY id")] == [1.0, 1.0, 0.0]


def test_weak_topics_ranked_by_score_and_split_by_kind(setup):
    conn, add = setup
    lec = add("L1", "lecture", "C", "s")
    lab_ = add("Lab1", "lab", "C", "s")
    exam.store_questions(conn, lec, "lecture", [{"topic": "Keys", "question": "q1", "answer": "a"},
                                               {"topic": "Joins", "question": "q2", "answer": "a"},
                                               {"topic": "SQL", "question": "q3", "answer": "a"}])
    exam.store_questions(conn, lab_, "lab", [{"topic": "Cursors", "question": "q4", "answer": "a"}])
    q = {r["question"]: r["id"] for r in conn.execute("SELECT id, question FROM quiz_questions")}
    exam.record_answer(conn, q["q1"], "wrong", NOW)
    exam.record_answer(conn, q["q2"], "partly", NOW)
    exam.record_answer(conn, q["q3"], "correct", NOW)
    exam.record_answer(conn, q["q4"], "wrong", NOW)
    weak = exam.weak_topics(conn, "lecture", NOW - timedelta(days=7))
    assert [t for t, _, _ in weak] == ["Keys", "Joins"]  # fully-correct topics aren't weak
    assert [t for t, _, _ in exam.weak_topics(conn, "lab", NOW - timedelta(days=7))] == ["Cursors"]
    assert exam.weak_topics(conn, "lecture", NOW + timedelta(days=1)) == []


# --- full quiz round through the orchestrator ---

async def test_full_quiz_round(setup, tmp_path):
    conn, add = setup
    add("week2lecture2_Normalization", "lecture", "CSCI235", "1NF, keys")
    mark_ok = '{"verdict": "correct", "explanation": "Right."}'
    mark_wrong = '{"verdict": "wrong", "explanation": "No."}'
    o = Orchestrator(Settings(data_dir=tmp_path), conn, llm_with(conn, [QS, mark_ok, mark_wrong]))

    first = await o.quiz_start("lecture", "normalization")
    assert "2 questions" in first and "Q1" in first
    second = await o.quiz_answer("atomic values")
    assert second.startswith("✅ Correct") and "Q2" in second
    end = await o.quiz_answer("no idea")
    assert end.startswith("❌ Not quite") and "Model answer:" in end and "1 / 2 (50%)" in end
    assert o.quiz is None
    assert await o.quiz_answer("stray message") is None

    # Questions are reused next time, no new generation call (3 calls total so far).
    assert len(o.llm.provider.calls) == 3
    o.llm.provider.replies.append(mark_ok)
    await o.quiz_start("lecture", None)
    assert len(o.llm.provider.calls) == 3
    assert "Q2" in o.quiz_skip()
    assert o.quiz_stop() == "🏁 Quiz finished: 0 / 1 (0%)."  # skip = wrong; unanswered Q2 not counted


async def test_quiz_on_unknown_topic_lists_what_exists(setup, tmp_path):
    conn, add = setup
    add("Lecture 1 - Intro to PM", "lecture", "CSIT214", "Project charters")
    o = Orchestrator(Settings(data_dir=tmp_path), conn, llm_with(conn, []))
    msg = await o.quiz_start("lecture", "quantum")
    assert "No lecture summaries match 'quantum'" in msg and "CSIT214: Lecture 1 - Intro to PM" in msg
    assert "Send one with /lab" in await o.quiz_start("lab", None)
