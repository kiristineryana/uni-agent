"""Error alerts to Telegram, rate-limited per error kind so an outage can't flood the phone."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Callable

from telegram.error import NetworkError, RetryAfter

from . import db, notify

log = logging.getLogger(__name__)

# Expected while offline or asleep; the app recovers on its own, so these are logged, not alerted.
TRANSIENT = (NetworkError, RetryAfter, TimeoutError, ConnectionError)


def is_transient(exc: BaseException) -> bool:
    from telegram.error import BadRequest

    return isinstance(exc, TRANSIENT) and not isinstance(exc, BadRequest)


class ErrorReporter:
    def __init__(self, conn: sqlite3.Connection, cooldown: timedelta = timedelta(hours=1),
                 clock: Callable[[], datetime] = db.utcnow):
        self.conn = conn
        self.cooldown = cooldown
        self.clock = clock
        self._last: dict[str, datetime] = {}

    def should_report(self, key: str) -> bool:
        now = self.clock()
        last = self._last.get(key)
        if last is not None and now - last < self.cooldown:
            return False
        self._last[key] = now
        return True

    async def report(self, key: str, text: str) -> bool:
        """Record and (if not in cooldown) send. Never raises: an error alert must not cause new errors."""
        db.kv_set(self.conn, "last_error", f"{db.iso(self.clock())} {text[:200]}")
        if not self.should_report(key):
            return False
        try:
            await notify.send_telegram(f"⚠️ Uni Agent problem: {text}")
            return True
        except Exception as e:  # e.g. still offline
            log.warning("Could not deliver error alert (%s)", type(e).__name__)
            self._last.pop(key, None)  # try again next time
            return False

    async def report_exception(self, where: str, exc: BaseException) -> bool:
        if is_transient(exc):
            log.warning("%s: transient %s (not alerted)", where, type(exc).__name__)
            return False
        log.error("%s failed", where, exc_info=exc)
        return await self.report(f"{where}:{type(exc).__name__}", f"{where} failed ({type(exc).__name__}). "
                                                                   "Details in logs\\uni_agent.log.")
