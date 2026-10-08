"""Local speech-to-text for voice notes and audio files, with faster-whisper (runs on the CPU, offline after a
one-time model download into data/models). `get_transcriber()` returns path -> text, or None if unavailable."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

Transcriber = Callable[[Path], str]
SAMPLE_RATE = 16_000  # what Whisper models expect


def decode_audio(path: Path):
    """Any audio (Telegram OGG/Opus, mp3, m4a, wav...) -> mono float32 numpy array at 16 kHz.
    Done here instead of faster-whisper's own decoder, which passes an option newer PyAV versions removed."""
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks = []
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def make_transcriber(model_name: str, models_dir: Path, language: str | None) -> Transcriber | None:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    lock = threading.Lock()
    model: list = []

    def transcribe(path: Path) -> str:
        with lock:  # one transcription at a time; the model is loaded on first use (first run downloads it)
            if not model:
                log.info("Loading speech model %s", model_name)
                try:  # use the downloaded copy without touching the network
                    m = WhisperModel(model_name, device="cpu", compute_type="int8",
                                     download_root=str(models_dir), local_files_only=True)
                except Exception:
                    log.info("Downloading speech model %s (~150 MB, one time)", model_name)
                    m = WhisperModel(model_name, device="cpu", compute_type="int8", download_root=str(models_dir))
                model.append(m)
            audio = decode_audio(path)
            if audio.size == 0:
                return ""
            segments, info = model[0].transcribe(audio, language=language, beam_size=5, vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
            log.info("Transcribed %.0fs of audio (%d chars)", info.duration, len(text))
            return text

    return transcribe
