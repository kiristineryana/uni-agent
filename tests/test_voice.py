"""Voice notes: decoding, transcript -> document, and voice answers in quizzes."""
import json
import math
import struct
import wave
from pathlib import Path

import pytest

from uni_agent import db, notify
from uni_agent.agents import exam, ingest
from uni_agent.config import Settings
from uni_agent.extract import VOICE_MARK
from uni_agent.llm import LLM, LLMResult
from uni_agent.orchestrator import Orchestrator
from uni_agent.transcribe import SAMPLE_RATE, decode_audio

LONG = ("Today we covered second normal form. A table is in second normal form when it is in first normal form "
        "and every non key attribute depends on the whole primary key, not just part of a composite key.")


def tone_wav(path: Path, seconds=1.5, rate=44100) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(struct.pack("<hh", s, s) for s in
                          (int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate))))
        w.writeframes(frames)
    return path


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


@pytest.fixture
def orch(tmp_path):
    o = Orchestrator(Settings(data_dir=tmp_path / "data", voice_enabled=False), db.connect(":memory:"))
    o.uploaded = []

    async def fake_upload(path, kind, course=None):
        o.uploaded.append((path, kind, course))

    o.process_upload = fake_upload
    return o


def test_decode_audio_resamples_to_16k_mono(tmp_path):
    audio = decode_audio(tone_wav(tmp_path / "t.wav", seconds=1.5))
    assert audio.ndim == 1 and audio.dtype.name == "float32"
    assert abs(len(audio) - 1.5 * SAMPLE_RATE) < 0.05 * SAMPLE_RATE
    assert 0.1 < float(abs(audio).max()) <= 1.0


async def test_long_voice_note_becomes_a_document(orch, phone, tmp_path):
    rec = tmp_path / "voice_5.ogg"
    rec.write_bytes(b"audio")
    orch.transcriber = lambda p: LONG
    await orch.process_voice(rec, "lecture", "CSCI235", "2NF recap")
    [(path, kind, course)] = orch.uploaded
    assert (path.name, kind, course) == ("2NF recap.txt", "lecture", "CSCI235")
    assert VOICE_MARK in path.read_text(encoding="utf-8")
    assert phone[0] == "🎙️ Transcribing…"


async def test_short_voice_note_just_gets_the_transcript(orch, phone, tmp_path):
    rec = tmp_path / "voice_6.ogg"
    rec.write_bytes(b"audio")
    orch.transcriber = lambda p: "Remember to revise joins."
    await orch.process_voice(rec, None, None)
    assert orch.uploaded == []
    assert "Remember to revise joins." in phone[-1] and "Too short to summarise" in phone[-1]


async def test_silence_and_disabled_voice(orch, phone, tmp_path):
    rec = tmp_path / "voice_7.ogg"
    rec.write_bytes(b"audio")
    orch.transcriber = lambda p: ""
    await orch.process_voice(rec, None, None)
    assert "couldn't hear any speech" in phone[-1]
    orch.transcriber = None
    await orch.process_voice(rec, None, None)
    assert "needs speech recognition" in phone[-1] and orch.uploaded == []


async def test_voice_answer_in_a_quiz_is_transcribed_then_marked(tmp_path):
    conn = db.connect(":memory:")
    summary = tmp_path / "s.md"
    summary.write_text("2NF", encoding="utf-8")
    ingest.record_document(conn, tmp_path / "week2lecture2.pdf", "sha", "lecture", "CSCI235", summary)

    class Provider:
        name = "fake"
        replies = [json.dumps([{"topic": "2NF", "question": "What is 2NF?", "answer": "No partial dependencies."}]),
                   '{"verdict": "correct", "explanation": "Yes."}']
        prompts = []

        def complete(self, system, prompt, max_tokens):
            self.prompts.append(prompt)
            return LLMResult(self.replies.pop(0), 1, 1)

    s = Settings(data_dir=tmp_path / "data", voice_enabled=False)
    o = Orchestrator(s, conn, LLM(Provider(), conn, 10**9, s.tz))
    o.transcriber = lambda p: "no partial dependencies on the key"
    assert not o.awaiting_quiz_answer()
    await o.quiz_start("lecture", None)
    assert o.awaiting_quiz_answer()
    reply = await o.quiz_voice_answer(tmp_path / "v.ogg")
    assert reply.startswith("🎙️ I heard: “no partial dependencies on the key”") and "✅ Correct" in reply
    assert "no partial dependencies on the key" in Provider.prompts[-1]  # the transcript was what got marked
    assert exam.weak_topics(conn, "lecture", db.utcnow().replace(year=2000)) == []


async def test_voice_answer_with_no_speech_keeps_the_question(tmp_path):
    o = Orchestrator(Settings(data_dir=tmp_path, voice_enabled=False), db.connect(":memory:"))
    o.transcriber = lambda p: ""
    assert "couldn't hear any speech" in await o.quiz_voice_answer(tmp_path / "v.ogg")
