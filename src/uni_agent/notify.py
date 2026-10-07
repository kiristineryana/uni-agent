"""The only module (besides the bot handlers) that talks to Telegram. Splits long messages."""
from __future__ import annotations

import asyncio
import logging
import warnings

from telegram import Bot
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.warnings import PTBDeprecationWarning

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split text into chunks <= limit, preferring paragraph, then line, then word boundaries."""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = max(window.rfind("\n\n"), 0)
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class Notifier:
    def __init__(self, bot: Bot, chat_id: int, attempts: int = 4, sleep=asyncio.sleep):
        self.bot = bot
        self.chat_id = chat_id
        self.attempts = attempts
        self._sleep = sleep

    async def send(self, text: str, **kwargs) -> None:
        parts = split_message(text)
        for i, part in enumerate(parts):
            # Reply markup (buttons) only goes on the last chunk.
            extra = kwargs if i == len(parts) - 1 else {}
            await self._send_with_retry(part, extra)

    async def _send_with_retry(self, text: str, extra: dict) -> None:
        """Retry network blips with backoff and honour Telegram's flood control; never retry bad requests."""
        delay = 2.0
        for attempt in range(1, self.attempts + 1):
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=text, **extra)
                return
            except BadRequest:
                raise  # (subclass of NetworkError in PTB) retrying won't help
            except RetryAfter as e:
                with warnings.catch_warnings():  # int now, timedelta in a future PTB; both handled
                    warnings.simplefilter("ignore", PTBDeprecationWarning)
                    wait = e.retry_after
                wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
                if attempt == self.attempts:
                    raise
                await self._sleep(float(wait) + 1)
            except NetworkError as e:  # includes TimedOut
                if attempt == self.attempts:
                    raise
                log.warning("Telegram send failed (%s), retry %d/%d in %.0fs",
                            type(e).__name__, attempt, self.attempts - 1, delay)
                await self._sleep(delay)
                delay *= 2


_notifier: Notifier | None = None


def configure(notifier: Notifier) -> None:
    global _notifier
    _notifier = notifier


async def send_telegram(text: str, **kwargs) -> None:
    if _notifier is None:
        log.warning("Notifier not configured; dropping message: %.60s", text)
        return
    await _notifier.send(text, **kwargs)
