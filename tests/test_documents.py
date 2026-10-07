import asyncio
from pathlib import Path

import pytest

from uni_agent import db
from uni_agent.agents import ingest, lab, lecture
from uni_agent.config import Settings
from uni_agent.extract import chunk_text, has_real_text, sha256_file
from uni_agent.llm import LLM, BudgetExceeded, LLMResult
from uni_agent.orchestrator import Orchestrator
from uni_agent.sources import folder_watch
from uni_agent.sources.folder_watch import FoundFile, guess_kind, is_candidate

COURSE = Path("C:/UOWD/235")


# --- kind guessing (real filenames from the course folders) ---

@pytest.mark.parametrize("name,kind", [
    ("CSIT205_L01.pdf", "lecture"),
    ("Lecture 1 - Introduction to PM.pptx", "lecture"),
    ("week1lecture2.pptx", "lecture"),
    ("week2lecture2_Normalization.pptx", "lecture"),
    ("Practice 235 WEEK 2.docx", "lab"),
    ("CSCI235_Lab_4_sheet.pdf", "lab"),
    ("CSIT 123 tutorial-8-week9.pdf", "lab"),
    ("week3lab2.pdf", "lab"),
    ("Subject syllabus.pdf", None),            # 'lab' inside 'syllabus' must not count
    ("Collaboration notes.docx", None),
    ("Lecture 5 lab demo.pptx", None),         # both -> ask
])
def test_guess_kind_from_filename(name, kind):
    assert guess_kind(COURSE / name, COURSE) == kind


def test_folder_overrides_filename():
    assert guess_kind(COURSE / "labs" / "Lecture recap.pdf", COURSE) == "lab"
    assert guess_kind(COURSE / "Lectures" / "lab demo.pdf", COURSE) == "lecture"


@pytest.mark.parametrize("name,ok", [
    ("CSIT205_L01.pdf", True),
    ("Week_1_CSCI235_Lab_1_1234567_StudentName.docx", False),  # own submission
    ("~$week1lecture1.pptx", False),
    ("notes.pdf.crdownload", False),
    ("diagram.drawio", False),
])
def test_is_candidate(name, ok):
    assert is_candidate(Path(name), ["*1234567*", "~$*"]) == ok


# --- hashing / extraction helpers ---

def test_sha256_stable_and_content_based(tmp_path):
    a, b, c = tmp_path / "a.txt", tmp_path / "b.txt", tmp_path / "c.txt"
    a.write_bytes(b"same")
    b.write_bytes(b"same")
    c.write_bytes(b"different")
    assert sha256_file(a) == sha256_file(b) != sha256_file(c)
    assert len(sha256_file(a)) == 64


def test_has_real_text():
    assert not has_real_text("## Slide 1\n\n## Slide 2\n")
    assert has_real_text("## Slide 1\n" + "Normalization removes redundancy. " * 10)


def test_chunk_text_keeps_sections_and_limit():
    text = "\n".join(f"## Slide {i}\n" + f"content {i} " * 30 for i in range(1, 40))
    chunks = chunk_text(text, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert all(c.startswith("## Slide") for c in chunks)  # never splits mid-slide here
    assert "".join(chunks).count("## Slide") == 39


def test_chunk_text_splits_oversized_section():
    text = "## Page 1\n" + "\n".join("x" * 50 for _ in range(100))
    chunks = chunk_text(text, 600)
    assert len(chunks) > 1 and all(len(c) <= 600 for c in chunks)


# --- LLM cap ---

class FakeProvider:
    name = "fake"

    def __init__(self, reply="ok", tokens=(100, 50)):
        self.reply, self.tokens, self.calls = reply, tokens, []

    def complete(self, system, prompt, max_tokens):
        self.calls.append(prompt)
        return LLMResult(self.reply, *self.tokens)


def make_llm(cap=1000, **kw):
    conn = db.connect(":memory:")
    return LLM(FakeProvider(**kw), conn, cap, Settings().tz)


def test_llm_records_usage_and_enforces_cap():
    llm = make_llm(cap=300)
    llm.complete("s", "p")
    llm.complete("s", "p")
    assert llm.used_today() == 300
    with pytest.raises(BudgetExceeded):
        llm.complete("s", "p")
    assert len(llm.provider.calls) == 2


# --- lecture agent ---

def test_lecture_single_request_for_normal_docs():
    llm = make_llm(cap=10**9, reply="📘 summary")
    out = lecture.summarize(llm, "## Slide 1\nNormalization", "Week 2", "CSCI235", ["Week 1"], 10_000)
    assert out == "📘 summary"
    assert len(llm.provider.calls) == 1
    assert "Week 1" in llm.provider.calls[0] and "CSCI235" in llm.provider.calls[0]


def test_lecture_chunks_long_docs_then_combines():
    llm = make_llm(cap=10**9, reply="notes")
    text = "\n".join(f"## Slide {i}\n" + "theory " * 100 for i in range(20))
    lecture.summarize(llm, text, "Big", "CSIT205", [], 3000)
    calls = llm.provider.calls
    assert len(calls) > 2
    assert "NOTES FROM ALL PARTS" in calls[-1]


# --- ingest helpers ---

def test_guess_course_for_uploads():
    names = {"205": "CSIT205", "214": "CSIT214", "235": "CSCI235"}
    assert ingest.guess_course("CSIT205_L03.pdf", names) == "CSIT205"
    assert ingest.guess_course("235 week 4 lecture.pptx", names) == "CSCI235"
    assert ingest.guess_course("Lecture 2052.pdf", names) is None
    assert ingest.guess_course("random.pdf", names) is None


# --- routing / baseline ---

@pytest.fixture
def orch(tmp_path):
    course = tmp_path / "235"
    course.mkdir()
    s = Settings(course_folders=[course], course_names={"235": "CSCI235"}, data_dir=tmp_path / "data",
                 ignore_patterns=["*1234567*"])
    o = Orchestrator(s, db.connect(":memory:"))
    o.routed = []

    async def fake_process(path, kind, course, sha, from_upload=False):
        o.routed.append((path.name, kind, course))
        ingest.record_document(o.conn, path, sha, kind, course, None)

    o.process_document = fake_process
    return o, course


async def test_lecture_file_routes_to_lecture_agent(orch):
    o, course = orch
    f = course / "week3lecture1.pptx"
    f.write_bytes(b"lecture bytes")
    await o.on_new_file(FoundFile(f, course, guess_kind(f, course)))
    assert o.routed == [("week3lecture1.pptx", "lecture", "CSCI235")]


async def test_file_in_labs_folder_never_reaches_lecture_agent(orch):
    o, course = orch
    (course / "labs").mkdir()
    f = course / "labs" / "Week 3 Lecture recap.pdf"  # misleading name, but it's in labs/
    f.write_bytes(b"lab bytes")
    await o.on_new_file(FoundFile(f, course, guess_kind(f, course)))
    assert o.routed == [("Week 3 Lecture recap.pdf", "lab", "CSCI235")]


async def test_duplicate_content_processed_once(orch):
    o, course = orch
    for name in ["week1lecture1.pptx", "week1lecture1 (copy).pptx"]:
        (course / name).write_bytes(b"identical")
    await o.scan_folders()
    await o.scan_folders()
    assert len(o.routed) == 1


async def test_baseline_marks_existing_files_without_processing(orch):
    o, course = orch
    (course / "week1lecture1.pptx").write_bytes(b"old lecture")
    (course / "Lab_1234567.docx").write_bytes(b"my submission")
    assert o.baseline_folders() == 1  # own submission ignored
    assert o.baseline_folders() == 0  # only once per folder
    await o.scan_folders()
    assert o.routed == []
    (course / "week2lecture1.pptx").write_bytes(b"new lecture")
    await o.scan_folders()
    assert [r[0] for r in o.routed] == ["week2lecture1.pptx"]


async def test_lab_named_file_routes_to_lab_agent(orch):
    o, course = orch
    f = course / "CSCI235_Lab_4_sheet.pdf"
    f.write_bytes(b"lab sheet")
    await o.scan_folders()
    assert o.routed == [("CSCI235_Lab_4_sheet.pdf", "lab", "CSCI235")]


async def test_unsure_file_is_asked_once_then_routed_by_button(orch):
    o, course = orch
    f = course / "Week 4 notes.pdf"  # no lecture/lab hint
    f.write_bytes(b"ambiguous")
    await o.scan_folders()
    await o.scan_folders()  # periodic rescan must not ask again or process it
    assert o.routed == []
    row = o.conn.execute("SELECT id, status FROM documents").fetchone()
    assert row["status"] == "pending_kind"

    assert "Lab" in await o.resolve_kind(row["id"], "lab")
    await asyncio.sleep(0)  # let the background task run
    assert o.routed == [("Week 4 notes.pdf", "lab", "CSCI235")]
    assert await o.resolve_kind(row["id"], "lecture") == "Already handled."


def test_course_from_arg(orch):
    o, _ = orch
    assert o.course_from_arg("235") == "CSCI235"
    assert o.course_from_arg("csci235") == "CSCI235"
    assert o.course_from_arg("PROJ085") == "PROJ085"
    assert o.course_from_arg(None) is None


# --- real dispatch: the right agent's prompt reaches the LLM ---

class RecordingProvider(FakeProvider):
    def complete(self, system, prompt, max_tokens):
        self.calls.append((system, prompt))
        return LLMResult("summary text", 10, 10)


@pytest.mark.parametrize("kind,agent", [("lecture", lecture), ("lab", lab)])
async def test_process_document_uses_the_agent_for_its_kind(tmp_path, kind, agent):
    conn = db.connect(":memory:")
    s = Settings(data_dir=tmp_path / "data")
    provider = RecordingProvider()
    o = Orchestrator(s, conn, LLM(provider, conn, 10**9, s.tz))
    f = tmp_path / "doc.txt"
    f.write_text("## Section\n" + "Normalization and functional dependencies. " * 20)
    await o.process_document(f, kind, "CSCI235", "sha-" + kind)
    [(system, prompt)] = provider.calls
    assert system == agent.SYSTEM_PROMPT
    other = lab if agent is lecture else lecture
    assert system != other.SYSTEM_PROMPT
    row = conn.execute("SELECT kind, summary_path FROM documents").fetchone()
    assert row["kind"] == kind
    assert Path(row["summary_path"]).parent.parent.name == ("lectures" if kind == "lecture" else "labs")


def test_lab_prompt_has_integrity_rules_and_deadlines():
    llm = make_llm(cap=10**9, reply="🧪")
    lab.summarize(llm, "## Lab\nCreate tables", "Lab 4", "CSCI235", [], 10_000, ["Lab 4 Submission: Sun 11 Oct, 23:55"])
    prompt = llm.provider.calls[0]
    assert "Lab 4 Submission: Sun 11 Oct, 23:55" in prompt
    assert "Never write the graded answers" in lab.SYSTEM_PROMPT
    assert "Viva questions" in prompt and "Submission checklist" in prompt
