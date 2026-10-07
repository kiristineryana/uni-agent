"""Exam agent: generates questions from stored summaries, marks answers, and does Leitner scheduling.
Lectures -> theory questions; labs -> practical / viva-style questions. No Telegram; storage helpers at the bottom."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .. import db
from ..llm import LLM, LLMError

SYSTEM_PROMPT = """You are an exam coach for a computer science student.
You write short-answer questions and mark answers fairly, like a supportive university examiner.
Base everything on the study material provided. Answer with JSON only, no prose and no code fences."""

GEN_LECTURE = """Write {n} short-answer THEORY questions from this lecture summary: a mix of recall
("Define...", "List...", "What is...") and application ("Given..., what would...?", "Why does...?").
Each answer must be checkable in 1-3 sentences.

Return a JSON array: [{{"topic": "2-4 word topic label", "question": "...", "answer": "model answer, 1-3 sentences"}}]

LECTURE SUMMARY ({title}):
{summary}"""

GEN_LAB = """Write {n} PRACTICAL / VIVA-style questions from this lab briefing, like a lab demonstrator would ask:
"Why did we use X?", "What would happen if...?", "How would you check that...?", "What does this error usually mean?".
Test understanding of the procedure and concepts. Do NOT ask the student to write the lab's graded code or answers.
Each answer must be checkable in 1-3 sentences.

Return a JSON array: [{{"topic": "2-4 word topic label", "question": "...", "answer": "model answer, 1-3 sentences"}}]

LAB BRIEFING ({title}):
{summary}"""

MARK = """Mark the student's answer against the model answer. Be fair: accept correct ideas in different words;
ignore spelling. "partly" = some key idea right but something important missing or wrong.

Question: {question}
Model answer: {answer}
Student answer: {reply}

Return JSON: {{"verdict": "correct" | "partly" | "wrong", "explanation": "1-3 sentences: what was right, what was missing, the key point to remember"}}"""

SCORES = {"correct": 1.0, "partly": 0.5, "wrong": 0.0}
MAX_BOX = 5
BOX_INTERVAL_DAYS = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16}


def _parse_json(text: str):
    """Tolerate code fences or stray prose around the JSON."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"(\[.*\]|\{.*\})", cleaned, re.DOTALL)
        if not m:
            raise LLMError("The model didn't return valid JSON.")
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise LLMError("The model didn't return valid JSON.") from e


def generate_questions(llm: LLM, summary: str, kind: str, title: str, n: int = 8) -> list[dict]:
    template = GEN_LAB if kind == "lab" else GEN_LECTURE
    data = _parse_json(llm.complete(SYSTEM_PROMPT, template.format(n=n, title=title, summary=summary),
                                    max_tokens=4096))
    questions = []
    for q in data if isinstance(data, list) else []:
        if isinstance(q, dict) and q.get("question") and q.get("answer"):
            questions.append({"topic": str(q.get("topic") or title).strip()[:60],
                              "question": str(q["question"]).strip(),
                              "answer": str(q["answer"]).strip()})
    if not questions:
        raise LLMError("No usable questions were generated.")
    return questions


def mark_answer(llm: LLM, question: str, answer: str, reply: str) -> tuple[str, str]:
    data = _parse_json(llm.complete(SYSTEM_PROMPT, MARK.format(question=question, answer=answer, reply=reply),
                                    max_tokens=1024))
    verdict = str(data.get("verdict", "")).lower().strip() if isinstance(data, dict) else ""
    if verdict not in SCORES:
        verdict = "wrong" if not reply.strip() else "partly"
    explanation = str(data.get("explanation", "")).strip() if isinstance(data, dict) else ""
    return verdict, explanation


def next_box(box: int, verdict: str) -> int:
    """Leitner: correct moves up a box, wrong goes back to box 1, partly stays."""
    if verdict == "correct":
        return min(box + 1, MAX_BOX)
    if verdict == "wrong":
        return 1
    return box


def next_due(box: int, now: datetime) -> datetime:
    return now + timedelta(days=BOX_INTERVAL_DAYS[box])


VERDICT_ICON = {"correct": "✅ Correct", "partly": "🟡 Partly correct", "wrong": "❌ Not quite"}


def feedback_text(verdict: str, explanation: str, model_answer: str) -> str:
    text = f"{VERDICT_ICON[verdict]}\n{explanation}".strip()
    if verdict != "correct":
        text += f"\n\nModel answer: {model_answer}"
    return text


# --- storage helpers (quiz_questions / quiz_log) ---------------------------

def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", s.lower())


def read_summary(summary_path: str) -> str:
    text = Path(summary_path).read_text(encoding="utf-8")
    return text.split("\n---\nSource:", 1)[0].strip()


def matching_docs(conn: sqlite3.Connection, kind: str, topic: str | None) -> list[sqlite3.Row]:
    """Summarised docs of this kind whose course, filename or summary mentions the topic.
    'week 3' matches 'Week_3...'; '235' matches CSCI235; 'normalization' matches summary text."""
    rows = conn.execute(
        "SELECT * FROM documents WHERE kind = ? AND status = 'done' AND summary_path IS NOT NULL "
        "ORDER BY ingested_at", (kind,)).fetchall()
    if not topic:
        return rows
    t = _norm(topic)
    out = []
    for r in rows:
        haystack = _norm(f"{r['course']} {Path(r['path']).stem}")
        topics = [q["topic"] or "" for q in conn.execute(
            "SELECT DISTINCT topic FROM quiz_questions WHERE doc_id = ?", (r["id"],))]
        if t in haystack or any(t in _norm(x) for x in topics):  # weekly review suggests topic labels
            out.append(r)
            continue
        try:
            if topic.lower() in read_summary(r["summary_path"]).lower():
                out.append(r)
        except OSError:
            pass
    return out


def question_count(conn: sqlite3.Connection, doc_id: int) -> int:
    return conn.execute("SELECT COUNT(*) n FROM quiz_questions WHERE doc_id = ?", (doc_id,)).fetchone()["n"]


def store_questions(conn: sqlite3.Connection, doc_id: int, kind: str, questions: list[dict]) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO quiz_questions(doc_id, kind, topic, question, answer, box, next_due) "
            "VALUES (?, ?, ?, ?, ?, 1, NULL)",
            [(doc_id, kind, q["topic"], q["question"], q["answer"]) for q in questions],
        )


def pick_questions(conn: sqlite3.Connection, doc_ids: list[int], now: datetime, limit: int = 10) -> list[int]:
    """Due questions first (never asked, or next_due passed), lowest box first; then the rest as practice."""
    if not doc_ids:
        return []
    marks = ",".join("?" * len(doc_ids))
    rows = conn.execute(
        f"SELECT id FROM quiz_questions WHERE doc_id IN ({marks}) "
        "ORDER BY (next_due IS NULL OR next_due <= ?) DESC, box ASC, RANDOM() LIMIT ?",
        (*doc_ids, db.iso(now), limit),
    ).fetchall()
    return [r["id"] for r in rows]


def record_answer(conn: sqlite3.Connection, question_id: int, verdict: str, now: datetime) -> int:
    row = conn.execute("SELECT box FROM quiz_questions WHERE id = ?", (question_id,)).fetchone()
    box = next_box(row["box"], verdict)
    with conn:
        conn.execute("UPDATE quiz_questions SET box = ?, next_due = ? WHERE id = ?",
                     (box, db.iso(next_due(box, now)), question_id))
        conn.execute("INSERT INTO quiz_log(question_id, correct, answered_at) VALUES (?, ?, ?)",
                     (question_id, SCORES[verdict], db.iso(now)))
    return box


def weak_topics(conn: sqlite3.Connection, kind: str, since: datetime, limit: int = 3) -> list[tuple[str, float, int]]:
    """(topic, average score 0-1, answers) for the lowest-scoring topics answered since `since`."""
    rows = conn.execute(
        "SELECT q.topic, AVG(l.correct) avg, COUNT(*) n FROM quiz_log l "
        "JOIN quiz_questions q ON q.id = l.question_id "
        "WHERE q.kind = ? AND l.answered_at >= ? GROUP BY q.topic "
        "HAVING avg < 1 ORDER BY avg ASC, n DESC LIMIT ?",
        (kind, db.iso(since), limit),
    ).fetchall()
    return [(r["topic"], r["avg"], r["n"]) for r in rows]
