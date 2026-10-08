"""/files: a live record of the course folders, read from disk each time."""
import pytest

from uni_agent import db
from uni_agent.agents import files_index, ingest
from uni_agent.config import Settings
from uni_agent.extract import sha256_file
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

    text = o.files("235")
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
    assert "week3lecture1.pptx" in o.files("235")
    f.rename(c235 / "Week 3 - Joins.pptx")
    text = o.files("235")
    assert "Week 3 - Joins.pptx\n    ✅ summarised" in text and "week3lecture1" not in text  # same content, new name
    (c235 / "Week 3 - Joins.pptx").unlink()
    assert "0 files" in o.files("235") and "(empty)" in o.files("235")


def test_all_courses_capped_with_a_pointer_to_the_full_list(setup, monkeypatch):
    o, c235, c205 = setup
    monkeypatch.setattr(files_index, "PER_COURSE_LIMIT", 2)
    for i in range(4):
        write(c205 / f"CSIT205_L0{i}.pdf", f"l{i}".encode())
    text = o.files()
    assert "📁 CSIT205" in text and "📁 CSCI235" in text
    assert "… and 2 more: /files 205" in text
    assert text.count("CSIT205_L0") == 2


def test_unknown_course(setup):
    o, _, _ = setup
    assert o.files("999").startswith("No course folder for '999'")
