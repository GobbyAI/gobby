"""The CLI a deferred startup proved, which alone may create that session's row.

A fresh startup defers its row to the thread's first activity hook, and that hook is
where the session gets its id and its credential. The startup's kernel-proven CLI pid is
pinned here, so only a hook from that same CLI may create the row and receive the
token: a process that learns the thread id later cannot. The first pin for a thread
stands until its row is minted for or it ages out. Pins live in the daemon's memory, so
a restart before the first prompt leaves the thread unpinned, and it mints nothing until
its next SessionStart.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

PinKey = tuple[str, str, str]

_MAX_PINS = 4096
_MAX_AGE_SECONDS = 7 * 24 * 3600


class StartupPins:
    """Bounded, thread-safe map from (machine, source, provider session) to CLI pid."""

    def __init__(
        self, *, max_pins: int = _MAX_PINS, max_age_seconds: float = _MAX_AGE_SECONDS
    ) -> None:
        self._pins: OrderedDict[PinKey, tuple[int, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_pins = max_pins
        self._max_age = max_age_seconds

    def pin(self, key: PinKey, cli_pid: int) -> None:
        """Pin ``cli_pid`` for ``key`` unless a live pin already holds it."""
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            if key in self._pins:
                return
            self._pins[key] = (cli_pid, now)
            while len(self._pins) > self._max_pins:
                self._pins.popitem(last=False)

    def pinned(self, key: PinKey) -> int | None:
        """The CLI pid pinned for ``key``, if a live pin holds it."""
        with self._lock:
            self._expire(time.monotonic())
            entry = self._pins.get(key)
        return None if entry is None else entry[0]

    def release(self, key: PinKey) -> None:
        """Drop ``key``'s pin once its row exists and holds the pinned CLI."""
        with self._lock:
            self._pins.pop(key, None)

    def _expire(self, now: float) -> None:
        while self._pins:
            key, (_, pinned_at) = next(iter(self._pins.items()))
            if now - pinned_at <= self._max_age:
                return
            del self._pins[key]


_OWNER_ATTRIBUTE = "_session_startup_pins"
_owner_lock = threading.Lock()


def startup_pins(owner: object) -> StartupPins:
    """The pins kept for ``owner``, the daemon's HTTP server, created on first use."""
    with _owner_lock:
        pins = getattr(owner, _OWNER_ATTRIBUTE, None)
        if not isinstance(pins, StartupPins):
            pins = StartupPins()
            setattr(owner, _OWNER_ATTRIBUTE, pins)
    return pins
