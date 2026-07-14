"""Crash-loop breaker for native-crash-prone model loads.

A torch-MPS SIGSEGV kills the process before any Python except-handler runs,
so launchd KeepAlive respawns the daemon into the same crashing load forever
(incident #18196: three crashes in 75 seconds). The guard persists an attempt
marker (fsynced *before* the load starts) that only a completed load clears;
repeated mid-load deaths latch the loader into a cooldown.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_PERSISTED_ATTEMPTS = 10


class ModelLoadGuard:
    """Latches model loading after repeated attempts that never succeeded."""

    def __init__(
        self,
        path: Path,
        *,
        max_attempts: int = 3,
        window_seconds: float = 900.0,
        cooldown_seconds: float = 1800.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._now = now

    def _read_attempts(self) -> list[float]:
        try:
            entries = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(entries, list):
            return []
        timestamps: list[float] = []
        for entry in entries:
            ts = entry.get("ts") if isinstance(entry, dict) else None
            if isinstance(ts, int | float):
                timestamps.append(float(ts))
        return sorted(timestamps)

    def check(self) -> str | None:
        """Return a latch reason if loading is in cooldown, else None."""
        attempts = self._read_attempts()
        if not attempts:
            return None
        last = attempts[-1]
        now = self._now()
        recent = [ts for ts in attempts if last - ts <= self._window_seconds]
        if len(recent) < self._max_attempts:
            return None
        cooldown_until = last + self._cooldown_seconds
        if now >= cooldown_until:
            return None
        return (
            f"TTS model loading disabled: {len(recent)} load attempts died without "
            f"completing (likely a native torch/MPS crash). Retry allowed after "
            f"{time.strftime('%H:%M:%S', time.localtime(cooldown_until))}."
        )

    def record_attempt(self) -> None:
        """Persist an attempt marker durably before the load starts.

        Must survive a SIGSEGV during the load — write and fsync first.
        """
        attempts = self._read_attempts()
        attempts.append(self._now())
        attempts = attempts[-_MAX_PERSISTED_ATTEMPTS:]
        payload = json.dumps([{"ts": ts, "pid": os.getpid()} for ts in attempts])
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload.encode())
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            logger.warning("Failed to persist model load-guard marker", exc_info=True)

    def record_success(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to clear model load-guard marker", exc_info=True)


def default_tts_load_guard_path() -> Path:
    from gobby.paths import get_gobby_home

    return get_gobby_home() / "voice" / "tts_load_guard.json"
