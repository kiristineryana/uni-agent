"""Photos sent in Telegram: caption parsing, album batching, OCR -> document."""
import asyncio

import pytest

from uni_agent import db, notify
from uni_agent.bot import PhotoBatcher, parse_caption
from uni_agent.config import Settings
from uni_agent.extract import OCR_MARK
from uni_agent.orchestrator import Orchestrator

NAMES = {"205": "CSIT205", "214": "CSIT214", "235": "CSCI235"}


@pytest.fixture
def orch(tmp_path):
    o = Orchestrator(Settings(course_names=NAMES, data_dir=tmp_path / "data"), db.connect(":memory:"))
    o.uploaded = []

    async def fake_upload(path, kind, course=None):
        o.uploaded.append((path, kind, course))

    o.process_upload = fake_upload
    return o


@pytest.fixture
def phone():
    class Bot:
        sent = []

        async def send_message(self, chat_id, text, **kw):
            self.sent.append(text)

    bot = Bot()
    notify.configure(notify.Notifier(bot, 1))
    yield bot.sent
    notify.configure(None)


# --- caption parsing ---

@pytest.mark.parametrize("caption,expected", [
    ("/lab 235 Week 3 whiteboard", ("lab", "CSCI235", "Week 3 whiteboard")),
    ("/lecture CSIT205", ("lecture", "CSIT205", None)),
    ("/lecture Week 3 notes", ("lecture", None, "Week 3 notes")),  # 'Week' is not a course
    ("Normalization board", (None, None, "Normalization board")),
    ("214", (None, "CSIT214", None)),
    (None, (None, None, None)),
])
def test_parse_caption(orch, caption, expected):
    assert parse_caption(caption, orch.course_from_arg) == expected


# --- album batching ---

def make_batcher(tmp_path, wait=0.2):
    ready = []

    async def on_ready(paths, meta):
        ready.append(([p.name for p in paths], meta))

    return PhotoBatcher(on_ready, lambda: tmp_path, wait=wait), ready


def downloader(name, delay=0.0):
    async def download(folder):
        await asyncio.sleep(delay)
        path = folder / name
        path.write_bytes(b"img")
        return path
    return download


async def test_single_photo_is_one_document_immediately(tmp_path):
    b, ready = make_batcher(tmp_path)
    await b.add(None, 7, {"kind": "lab", "course": None, "title": None}, downloader("p7.jpg"))
    await asyncio.sleep(0.05)
    assert ready == [(["p7.jpg"], {"kind": "lab", "course": None, "title": None})]


async def test_album_becomes_one_document_in_order_with_merged_caption(tmp_path):
    b, ready = make_batcher(tmp_path)
    none = {"kind": None, "course": None, "title": None}
    await b.add("g1", 12, none, downloader("p12.jpg"))
    await asyncio.sleep(0.1)  # photos arrive a moment apart
    await b.add("g1", 11, {"kind": "lecture", "course": "CSCI235", "title": "Board"}, downloader("p11.jpg"))
    await b.add("g1", 13, none, downloader("p13.jpg"))
    assert ready == []  # still waiting for the quiet period
    await asyncio.sleep(0.5)
    assert ready == [(["p11.jpg", "p12.jpg", "p13.jpg"], {"kind": "lecture", "course": "CSCI235", "title": "Board"})]
    assert b.albums == {}


async def test_slow_download_is_not_dropped(tmp_path):
    b, ready = make_batcher(tmp_path, wait=0.1)
    none = {"kind": None, "course": None, "title": None}
    await b.add("g2", 1, none, downloader("p1.jpg"))
    slow = asyncio.ensure_future(b.add("g2", 2, none, downloader("p2.jpg", delay=0.4)))  # longer than `wait`
    await asyncio.sleep(0.3)
    assert ready == []  # must not flush while a download is still running
    await slow
    await asyncio.sleep(0.3)
    assert ready and ready[0][0] == ["p1.jpg", "p2.jpg"]


async def test_two_albums_stay_separate(tmp_path):
    b, ready = make_batcher(tmp_path, wait=0.1)
    none = {"kind": None, "course": None, "title": None}
    await b.add("a", 1, none, downloader("a1.jpg"))
    await b.add("b", 2, none, downloader("b2.jpg"))
    await asyncio.sleep(0.4)
    assert sorted(r[0][0] for r in ready) == ["a1.jpg", "b2.jpg"]


# --- OCR -> document ---

async def test_photos_are_ocred_into_one_text_document(orch, phone, tmp_path):
    pages = [tmp_path / "photo_1.jpg", tmp_path / "photo_2.jpg"]
    for p in pages:
        p.write_bytes(b"img")
    orch.ocr = lambda data: "Second normal form removes partial dependencies on a composite key. " * 2
    await orch.process_photos(pages, "lecture", "CSCI235", "Week 3 board")
    [(path, kind, course)] = orch.uploaded
    assert (path.name, kind, course) == ("Week 3 board.txt", "lecture", "CSCI235")
    text = path.read_text(encoding="utf-8")
    assert text.count(OCR_MARK) == 2 and "## Photo 2" in text
    assert phone[0] == "📷 Reading 2 photos as one document…"


async def test_unreadable_photo_gives_tips_and_no_document(orch, phone, tmp_path):
    p = tmp_path / "photo_1.jpg"
    p.write_bytes(b"img")
    orch.ocr = lambda data: "a b"
    await orch.process_photos([p], None, None)
    assert orch.uploaded == []
    assert "couldn't read enough text in that photo" in phone[-1] and "File" in phone[-1]


async def test_untitled_photo_gets_a_dated_name_and_unknown_kind_is_asked(orch, phone, tmp_path):
    p = tmp_path / "photo_1.jpg"
    p.write_bytes(b"img")
    orch.ocr = lambda data: "Relational algebra: selection, projection, join and division operators. " * 3
    await orch.process_photos([p], None, None, title='bad/name:"x"')
    [(path, kind, course)] = orch.uploaded
    assert path.name == "badnamex.txt" and kind is None  # process_upload(kind=None) asks Lecture or Lab


async def test_photos_without_ocr_explain_why(orch, phone, tmp_path):
    orch.ocr = None
    await orch.process_photos([tmp_path / "x.jpg"], "lab", None)
    assert "needs OCR" in phone[-1] and orch.uploaded == []
