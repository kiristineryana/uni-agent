"""/files: a live record of the course folders, read from disk each time."""
import asyncio

import pytest

from uni_agent import db
from uni_agent.agents import files_index, ingest
from uni_agent.config import Settings
from uni_agent.extract import sha256_file
from uni_agent.llm import LLMError
from uni_agent.orchestrator import Orchestrator


@pytest.fixture
def setup(tmp_path):
    c235, c205 = tmp_path / "235", tmp_path / "205"
    c235.mkdir()
    c205.mkdir()
    s = Settings(course_folders=[c205, c235], course_names={"205": "CSIT205", "235": "CSCI235"},
                 ignore_patterns=["*1234567*"], data_dir=tmp_path / "data")
    o = Orchestrator(s, db.connect(":memory:"))
    return o, c235, c205


def write(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_statuses_reflect_what_the_agent_knows(setup):
    o, c235, _ = setup
    done = write(c235 / "week2lecture2.pptx", b"lecture")
    ingest.record_document(o.conn, done, sha256_file(done), "lecture", "CSCI235", c235 / "s.md")
    old = write(c235 / "week1lecture1.pptx", b"old")
    ingest.record_document(o.conn, old, sha256_file(old), "lecture", "CSCI235", None, status="baseline")
    ask = write(c235 / "notes.pdf", b"ask")
    ingest.record_document(o.conn, ask, sha256_file(ask), None, "CSCI235", None, status="pending_kind")
    write(c235 / "labs" / "Lab4.docx", b"new lab")
    write(c235 / "Week_1_Lab_1234567.docx", b"mine")
    write(c235 / "diagram.drawio", b"d")
    write(c235 / "~$week2lecture2.pptx", b"lock")  # Office lock file: hidden

    text = o.files("235")[0]
    assert text.startswith("📁 CSCI235 (") and "6 files, 1 summarised" in text
    assert "📘 week2lecture2.pptx\n    ✅ summarised" in text
    assert "📘 week1lecture1.pptx\n    📦 here before the bot" in text
    assert "❓ waiting: Lecture or Lab?" in text
    assert "🧪 labs\\Lab4.docx" in text or "🧪 labs/Lab4.docx" in text
    assert "🙈 ignored (your own work)" in text and "📄 not a readable type" in text
    assert "~$" not in text


def test_record_is_live_renames_and_deletions_show_immediately(setup):
    o, c235, _ = setup
    f = write(c235 / "week3lecture1.pptx", b"content")
    ingest.record_document(o.conn, f, sha256_file(f), "lecture", "CSCI235", c235 / "s.md")
    assert "week3lecture1.pptx" in o.files("235")[0]
    f.rename(c235 / "Week 3 - Joins.pptx")
    text = o.files("235")[0]
    assert "Week 3 - Joins.pptx\n    ✅ summarised" in text and "week3lecture1" not in text  # same content, new name
    (c235 / "Week 3 - Joins.pptx").unlink()
    assert "0 files" in o.files("235")[0] and "(empty)" in o.files("235")[0]


def test_all_courses_capped_with_a_pointer_to_the_full_list(setup, monkeypatch):
    o, c235, c205 = setup
    monkeypatch.setattr(files_index, "PER_COURSE_LIMIT", 2)
    for i in range(4):
        write(c205 / f"CSIT205_L0{i}.pdf", f"l{i}".encode())
    text = o.files()[0]
    assert "📁 CSIT205" in text and "📁 CSCI235" in text
    assert "… and 2 more: /files 205" in text
    assert text.count("CSIT205_L0") == 2


def test_unknown_course(setup):
    o, _, _ = setup
    assert o.files("999")[0].startswith("No course folder for '999'")


# --- summarising from /files ---


@pytest.fixture
def routed(setup):
    o, c235, c205 = setup
    o.routed = []

    async def fake_process(path, kind, course, sha, from_upload=False):
        o.routed.append((path.name, kind, course, from_upload))
        ingest.record_document(o.conn, path, sha, kind, course, path.with_suffix(".md"))

    o.process_document = fake_process
    return o, c235, c205


def baseline(o, path, kind, course):
    ingest.record_document(o.conn, path, sha256_file(path), kind, course, None, status="baseline")
    return ingest.known_document(o.conn, sha256_file(path))["id"]


def test_buttons_only_for_files_without_a_summary(routed):
    o, c235, _ = routed
    old = baseline(o, write(c235 / "week1lecture1.pptx", b"old"), "lecture", "CSCI235")
    done = write(c235 / "week2lecture2.pptx", b"done")
    ingest.record_document(o.conn, done, sha256_file(done), "lecture", "CSCI235", c235 / "s.md")
    write(c235 / "week3lecture1.pptx", b"new")  # 🆕: the scan handles it, no button
    text, markup = o.files("235")
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["📘 CSCI235: week1lecture1.pptx", "Summarise all 1"]
    assert markup.inline_keyboard[0][0].callback_data == f"sum:{old}"
    assert "1 file(s) have no summary yet" in text and "Gemini request" in text


def test_no_buttons_when_everything_is_summarised(routed):
    o, c235, _ = routed
    f = write(c235 / "week2lecture2.pptx", b"done")
    ingest.record_document(o.conn, f, sha256_file(f), "lecture", "CSCI235", c235 / "s.md")
    assert o.files("235")[1] is None


async def test_button_sends_file_to_the_right_agent_once(routed):
    o, c235, _ = routed
    lab_id = baseline(o, write(c235 / "labs" / "Lab4.docx", b"lab"), "lab", "CSCI235")
    reply = await o.summarise_existing(lab_id)
    assert "🧪 Lab agent" in reply
    assert await o.summarise_existing(lab_id) == "Already handled."  # double tap
    await asyncio.sleep(0)
    assert o.routed == [("Lab4.docx", "lab", "CSCI235", True)]


async def test_unclear_kind_asks_lecture_or_lab(routed):
    o, c235, _ = routed
    doc_id = baseline(o, write(c235 / "Week 4 notes.pdf", b"?"), None, "CSCI235")
    assert "asking whether it's a lecture or a lab" in await o.summarise_existing(doc_id)
    status = o.conn.execute("SELECT status FROM documents WHERE id = ?", (doc_id,)).fetchone()["status"]
    assert status == "pending_kind" and o.routed == []


async def test_summarise_all_for_one_course(routed):
    o, c235, c205 = routed
    baseline(o, write(c235 / "week1lecture1.pptx", b"a"), "lecture", "CSCI235")
    baseline(o, write(c235 / "week1lecture2.pptx", b"b"), "lecture", "CSCI235")
    baseline(o, write(c205 / "CSIT205_L01.pdf", b"c"), "lecture", "CSIT205")
    reply = await o.summarise_all("CSCI235")
    await asyncio.sleep(0)
    assert reply.startswith("Queued 2 file(s)")
    assert sorted(r[0] for r in o.routed) == ["week1lecture1.pptx", "week1lecture2.pptx"]
    assert await o.summarise_all("CSCI235") == "Nothing left to summarise."


async def test_failed_summary_puts_the_button_back(setup, tmp_path):
    o, c235, _ = setup
    doc_id = baseline(o, write(c235 / "week1lecture1.txt", b"## S\n" + b"theory " * 60), "lecture", "CSCI235")

    class FailingLLM:
        def complete(self, *a, **k):
            raise LLMError("Gemini free-tier limit reached (429).")

    o.llm = FailingLLM()
    await o.summarise_existing(doc_id)
    for _ in range(20):
        await asyncio.sleep(0.05)
        status = o.conn.execute("SELECT status FROM documents WHERE id = ?", (doc_id,)).fetchone()["status"]
        if status != "queued_existing":
            break
    assert status == "baseline"
    assert o.files("235")[1] is not None  # button available again


def test_restart_releases_stuck_requests(tmp_path):
    conn = db.connect(":memory:")
    f = write(tmp_path / "235" / "x.pdf", b"x")
    ingest.record_document(conn, f, "sha", "lecture", "CSCI235", None, status="queued_existing")
    Orchestrator(Settings(data_dir=tmp_path / "data"), conn)
    assert conn.execute("SELECT status FROM documents").fetchone()["status"] == "baseline"
