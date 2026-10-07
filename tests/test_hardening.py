"""Phase 7: retries, error alerts, and surviving a network outage."""
from datetime import datetime, timedelta, timezone

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from uni_agent import db, notify
from uni_agent.agents import ingest
from uni_agent.config import Secrets, Settings
from uni_agent.errors import ErrorReporter, is_transient
from uni_agent.orchestrator import Orchestrator
from uni_agent.sources import moodle_ical
from uni_agent.sources.moodle_ical import FeedItem


class FlakyBot:
    """Raises the queued exceptions first, then succeeds."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(text)


async def no_sleep(_):
    pass


@pytest.fixture
def phone():
    """Configure notify with a controllable fake bot; restore afterwards."""
    bot = FlakyBot()
    notify.configure(notify.Notifier(bot, 1, attempts=3, sleep=no_sleep))
    yield bot
    notify.configure(None)


# --- Telegram send retries ---

async def test_send_retries_network_blips():
    bot = FlakyBot([NetworkError("down"), TimedOut()])
    await notify.Notifier(bot, 1, attempts=3, sleep=no_sleep).send("hi")
    assert bot.sent == ["hi"]


async def test_send_gives_up_after_attempts():
    bot = FlakyBot([NetworkError("down")] * 3)
    with pytest.raises(NetworkError):
        await notify.Notifier(bot, 1, attempts=3, sleep=no_sleep).send("hi")


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")  # RetryAfter(int) construction
async def test_send_honours_flood_control_and_never_retries_bad_request():
    waits = []

    async def record(s):
        waits.append(s)

    bot = FlakyBot([RetryAfter(5)])
    await notify.Notifier(bot, 1, attempts=3, sleep=record).send("hi")
    assert bot.sent == ["hi"] and waits == [6.0]

    bot = FlakyBot([BadRequest("chat not found")])
    with pytest.raises(BadRequest):
        await notify.Notifier(bot, 1, attempts=3, sleep=no_sleep).send("hi")
    assert bot.failures == []  # tried exactly once


# --- error reporter ---

def test_transient_classification():
    assert is_transient(NetworkError("x")) and is_transient(TimedOut()) and is_transient(ConnectionError())
    assert not is_transient(BadRequest("x")) and not is_transient(ValueError())


async def test_error_alerts_are_rate_limited_per_kind(phone):
    t = [datetime(2026, 10, 7, tzinfo=timezone.utc)]
    rep = ErrorReporter(db.connect(":memory:"), cooldown=timedelta(hours=1), clock=lambda: t[0])
    assert await rep.report_exception("Job ingest", ValueError("boom"))
    assert not await rep.report_exception("Job ingest", ValueError("boom"))  # within cooldown
    assert await rep.report_exception("Job ingest", KeyError("other"))  # different kind
    t[0] += timedelta(hours=2)
    assert await rep.report_exception("Job ingest", ValueError("boom"))
    assert len(phone.sent) == 3 and "Details in logs" in phone.sent[0]
    assert "boom" not in phone.sent[0]  # exception text (may contain URLs/tokens) is never sent


async def test_transient_errors_are_logged_not_alerted(phone):
    rep = ErrorReporter(db.connect(":memory:"))
    assert not await rep.report_exception("Telegram handler", NetworkError("offline"))
    assert phone.sent == []


async def test_failed_error_alert_does_not_raise_and_retries_later(phone):
    rep = ErrorReporter(db.connect(":memory:"))
    phone.failures = [NetworkError("down")] * 3
    assert not await rep.report("k", "problem")  # swallowed
    assert await rep.report("k", "problem")  # not stuck in cooldown since it was never delivered
    assert phone.sent == ["⚠️ Uni Agent problem: problem"]


# --- surviving an outage ---

async def test_deadline_alert_survives_outage_and_is_sent_exactly_once(phone):
    conn = db.connect(":memory:")
    o = Orchestrator(Settings(), conn)
    ingest.upsert_items(conn, [FeedItem("u1", "Assignment 2", "CSIT205", "assignment",
                                        db.utcnow() + timedelta(hours=2))])
    phone.failures = [NetworkError("down")] * 3  # network dies during the check
    with pytest.raises(NetworkError):
        await o.run_deadline_check()
    assert conn.execute("SELECT COUNT(*) n FROM alerts_sent").fetchone()["n"] == 0  # not marked as sent

    assert await o.run_deadline_check() == 1  # network back: delivered
    assert await o.run_deadline_check() == 0  # and never again
    assert len(phone.sent) == 1 and "Assignment 2" in phone.sent[0]


async def test_moodle_outage_alerts_once_then_reports_recovery(phone, monkeypatch):
    o = Orchestrator(Settings(secrets=Secrets(moodle_ical_url="https://example.invalid/feed")), db.connect(":memory:"))

    async def down(url):
        raise moodle_ical.FeedError("Moodle feed unavailable (ConnectError)")

    monkeypatch.setattr(moodle_ical, "fetch", down)
    for _ in range(5):
        assert (await o.run_ical_sync()).startswith("Moodle sync failed")
    alerts = [m for m in phone.sent if "Moodle sync has failed" in m]
    assert len(alerts) == 1 and "example.invalid" not in alerts[0]  # the private URL never leaks
    assert "Last problem" in o.status()

    async def up(url):
        return b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"

    monkeypatch.setattr(moodle_ical, "fetch", up)
    assert (await o.run_ical_sync()).startswith("Moodle sync OK")
    assert phone.sent[-1] == "✅ Moodle sync is working again."
    await o.run_ical_sync()
    assert phone.sent.count("✅ Moodle sync is working again.") == 1
