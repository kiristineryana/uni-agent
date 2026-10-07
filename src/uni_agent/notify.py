"""The only module (besides the bot handlers) that talks to Telegram. Splits long messages."""
from __future__ import annotations

import logging

from telegram import Bot

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
    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id

    async def send(self, text: str, **kwargs) -> None:
        parts = split_message(text)
        for i, part in enumerate(parts):
            # Reply markup (buttons) only goes on the last chunk.
            extra = kwargs if i == len(parts) - 1 else {}
            await self.bot.send_message(chat_id=self.chat_id, text=part, **extra)


_notifier: Notifier | None = None


def configure(notifier: Notifier) -> None:
    global _notifier
    _notifier = notifier


async def send_telegram(text: str, **kwargs) -> None:
    if _notifier is None:
        log.warning("Notifier not configured; dropping message: %.60s", text)
        return
    await _notifier.send(text, **kwargs)
