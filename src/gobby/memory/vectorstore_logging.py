"""Shared logging policy for vector-store consumers."""

from __future__ import annotations

import logging
import time

VECTORSTORE_WARNING_INTERVAL_SECONDS = 60.0


def log_rate_limited_warning(
    target_logger: logging.Logger,
    last_warned_at: float,
    message: str,
    error: BaseException,
) -> float:
    """Emit a transient-failure warning at the shared rate limit."""
    now = time.monotonic()
    if now - last_warned_at >= VECTORSTORE_WARNING_INTERVAL_SECONDS:
        target_logger.warning("%s: %s", message, error)
        return now
    target_logger.debug("%s: %s", message, error)
    return last_warned_at
