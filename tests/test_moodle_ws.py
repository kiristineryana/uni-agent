"""Moodle web-service file sync, against a fake Moodle (httpx.MockTransport)."""
import json
from pathlib import Path

import httpx
import pytest

from uni_agent import db
from uni_agent.agents import ingest, moodle_sync
from uni_agent.config import Secrets, Settings
from uni_agent.orchestrator import Orchestrator
from uni_agent.sources.moodle_ws import ALLOWED, MoodleWS, MoodleWSError, base_url_from, list_files

TOKEN = "secret-token-123"


class FakeMoodle:
    """Minimal Moodle: site info, my courses, course contents, pluginfile downloads."""

    def __init__(self):
        self.files = {  # fileurl -> bytes
            "https://m.test/webservice/pluginfile.php/1/L03.pdf": b"%PDF lecture 3",
            "https://m.test/webservice/pluginfile.php/2/Lab3.docx": b"lab three sheet",
            "https://m.test/webservice/pluginfile.php/3/notes.pdf": b"%PDF notes",
            "https://m.test/webservice/pluginfile.php/4/video.mp4": b"video",
        }
        self.timemodified = {u: 100 for u in self.files}
        self.calls = []
        self.error = None

    def contents(self, courseid):
        if courseid != 205:
            return []
        f = lambda url, name: {"type": "file", "filename": name, "fileurl": url,
                               "timemodified": self.timemodified[url], "filesize": len(self.files[url])}
        return [{"name": "Week 3", "modules": [
            {"modname": "resource", "name": "Lecture 3 slides", "contents": [f(list(self.files)[0], "L03.pdf")]},
            {"modname": "resource", "name": "Lab 3", "contents": [f(list(self.files)[1], "Lab3.docx")]},
            {"modname": "folder", "name": "Extra", "contents": [f(list(self.files)[2], "notes.pdf"),
                                                                f(list(self.files)[3], "video.mp4")]},
            {"modname": "url", "name": "Some link", "contents": [{"type": "url", "fileurl": "https://x"}]},
            {"modname": "forum", "name": "Announcements"},
        ]}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        if request.url.path.endswith("/webservice/rest/server.php"):
            assert q["wstoken"] == TOKEN
            fn = q["wsfunction"]
            self.calls.append(fn)
            if self.error:
                return httpx.Response(200, json={"exception": "x", "errorcode": self.error, "message": "no"})
            data = {"core_webservice_get_site_info": {"userid": 42},
                    "core_enrol_get_users_courses": [{"id": 205, "shortname": "CSIT205_DB426"},
                                                     {"id": 999, "shortname": "PROJ085_21"}],
                    "core_course_get_contents": self.contents(int(q.get("courseid", 0)))}[fn]
            return httpx.Response(200, json=data)
        url = str(request.url.copy_remove_param("token"))
        assert q.get("token") == TOKEN
        return httpx.Response(200, content=self.files[url], headers={"content-type": "application/octet-stream"})


@pytest.fixture
def env(tmp_path):
    course = tmp_path / "205"
    course.mkdir()
    s = Settings(course_folders=[course], course_names={"205": "CSIT205"}, data_dir=tmp_path / "data",
                 ignore_patterns=["*1234567*"])
    fake = FakeMoodle()
    ws = MoodleWS("https://m.test", TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return s, db.connect(":memory:"), fake, ws, course


def test_only_read_functions_are_allowed():
    assert ALLOWED == {"core_webservice_get_site_info", "core_enrol_get_users_courses", "core_course_get_contents"}


async def test_write_functions_are_refused_before_any_request(env):
    _, _, fake, ws, _ = env
    with pytest.raises(ValueError):
        await ws.call("mod_assign_save_submission")
    assert fake.calls == []


def test_base_url_and_file_listing():
    assert base_url_from("https://moodle.uowplatform.edu.au/calendar/export_execute.php?x=1") == \
        "https://moodle.uowplatform.edu.au"
    files = list_files(FakeMoodle().contents(205))
    assert [f.filename for f in files] == ["L03.pdf", "Lab3.docx", "notes.pdf", "video.mp4"]  # no links/forums


async def test_first_sync_saves_copies_without_summarising(env):
    s, conn, fake, ws, course = env
    r = await moodle_sync.sync_files(conn, ws, s)
    assert r.first_run and sorted(r.new_files) == ["CSIT205: L03.pdf", "CSIT205: Lab3.docx", "CSIT205: notes.pdf"]
    assert r.skipped_courses == ["PROJ085"]  # no folder for it
    assert (course / "Moodle" / "lectures" / "L03.pdf").read_bytes() == b"%PDF lecture 3"
    assert (course / "Moodle" / "labs" / "Lab3.docx").exists()
    assert (course / "Moodle" / "notes.pdf").exists()  # unclear kind: the bot will ask
    assert not list(course.rglob("*.mp4"))  # unsupported types skipped
    statuses = {r["status"] for r in conn.execute("SELECT status FROM documents")}
    assert statuses == {"baseline"}  # the watcher will treat them as already seen
    assert list((s.data_dir / "moodle_tmp").iterdir()) == []


async def test_later_sync_only_fetches_new_or_updated_files(env):
    s, conn, fake, ws, course = env
    await moodle_sync.sync_files(conn, ws, s)
    r = await moodle_sync.sync_files(conn, ws, s)
    assert r.new_files == [] and not r.first_run

    url = list(fake.files)[0]
    fake.files[url] = b"%PDF lecture 3 v2"
    fake.timemodified[url] = 200
    r = await moodle_sync.sync_files(conn, ws, s)
    assert r.new_files == ["CSIT205: L03 (2).pdf"]
    new_sha = ingest.known_document(conn, __import__("hashlib").sha256(b"%PDF lecture 3 v2").hexdigest())
    assert new_sha is None  # NOT pre-recorded: the folder watcher will summarise the update


async def test_content_already_in_folders_is_not_duplicated(env, tmp_path):
    s, conn, fake, ws, course = env
    mine = course / "L03 downloaded by me.pdf"
    mine.write_bytes(b"%PDF lecture 3")
    ingest.record_document(conn, mine, __import__("hashlib").sha256(b"%PDF lecture 3").hexdigest(),
                           "lecture", "CSIT205", None)
    r = await moodle_sync.sync_files(conn, ws, s)
    assert r.duplicates == 1 and not (course / "Moodle" / "lectures" / "L03.pdf").exists()


async def test_permanent_refusal_stops_and_never_leaks_the_token(env):
    s, conn, fake, ws, _ = env
    fake.error = "invalidtoken"
    with pytest.raises(MoodleWSError) as e:
        await moodle_sync.sync_files(conn, ws, s)
    assert e.value.permanent and TOKEN not in str(e.value)


async def test_orchestrator_turns_sync_off_after_refusal(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secrets=Secrets(moodle_token=TOKEN, moodle_ical_url="https://m.test/cal"))
    o = Orchestrator(s, db.connect(":memory:"))
    calls = []

    async def refuse(conn, ws, settings):
        calls.append(1)
        raise MoodleWSError("accessexception")

    monkeypatch.setattr(moodle_sync, "sync_files", refuse)
    assert "failed (accessexception)" in await o.run_moodle_file_sync()
    assert "off" in await o.run_moodle_file_sync() and calls == [1]  # no retry hammering
    assert "Moodle refused" in (db.kv_get(o.conn, "last_error") or "")
