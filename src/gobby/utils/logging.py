"""Logging helpers."""

from __future__ import annotations

import logging
import time
from typing import Any


class ThrottledLogger:
    """Log a message at most once per interval, keyed by rendered message."""

    def __init__(self, interval_seconds: float = 60.0) -> None:
        self._interval_seconds = interval_seconds
        self._last_logged: dict[str, float] = {}

    def __call__(self, log: logging.Logger, level: int, message: str, *args: Any) -> bool:
        """Log ``message`` if it was not logged within the interval.

        Returns True when the message was emitted, False when throttled.
        """
        key = message % args if args else message
        now = time.monotonic()
        last = self._last_logged.get(key)
        if last is not None and now - last < self._interval_seconds:
            return False
        self._last_logged[key] = now
        log.log(level, message, *args)
        return True
