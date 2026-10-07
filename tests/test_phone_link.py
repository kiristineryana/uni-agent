from uni_agent import db
from uni_agent.config import Settings
from uni_agent.notify import Notifier, split_message
from uni_agent.orchestrator import Orchestrator, is_allowed


def test_allowlist():
    assert is_allowed(42, 42)
    assert not is_allowed(43, 42)
    assert not is_allowed(None, 42)
    assert not is_allowed(42, None)


def test_split_short_message_untouched():
    assert split_message("hello") == ["hello"]


def test_split_respects_limit_and_keeps_content():
    paras = [("para %d " % i) * 40 for i in range(30)]
    text = "\n\n".join(paras)
    parts = split_message(text, limit=500)
    assert all(len(p) <= 500 for p in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(text.split())


def test_split_prefers_paragraph_boundary():
    text = "a" * 300 + "\n\n" + "b" * 300
    assert split_message(text, limit=400) == ["a" * 300, "b" * 300]


def test_split_hard_cuts_unbroken_text():
    parts = split_message("x" * 1000, limit=400)
    assert [len(p) for p in parts] == [400, 400, 200]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


async def test_notifier_sends_chunks_to_allowed_chat_with_markup_on_last():
    fake = FakeBot()
    await Notifier(fake, 7).send("a" * 5000, reply_markup="buttons")
    assert [c for c, _, _ in fake.sent] == [7, 7]
    assert fake.sent[0][2] == {} and fake.sent[1][2] == {"reply_markup": "buttons"}


def test_status_renders_on_empty_db():
    text = Orchestrator(Settings(), db.connect(":memory:")).status()
    assert "Last Moodle sync: never" in text
    assert "0 lectures, 0 labs" in text
