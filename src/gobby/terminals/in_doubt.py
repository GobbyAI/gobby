"""Process-level registry of terminal ids whose spawn outcome is still in doubt.

A spawn owner claims its terminal id before a prepare that may create a host
resource the rest of the daemon cannot yet see. While the id is held, no kill,
reaper or reconcile path may decide the row's outcome; they defer to the owner
instead. The registry is empty after a restart, which means no owner survived.

Every call is synchronous, short and never awaits. The registry lock is a leaf
lock: it is held only inside a registry call and never while a DB row lock or
``settle_lock`` is acquired. ``holds`` is also called from the DB worker thread.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable

DeferredStep = Callable[[], Awaitable[object]]


class InDoubtRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, list[DeferredStep]] = {}

    def claim(self, terminal_id: str) -> bool:
        """Take the id for one owner; false, with nothing changed, when it is held."""
        with self._lock:
            if terminal_id in self._held:
                return False
            self._held[terminal_id] = []
            return True

    def holds(self, terminal_id: str) -> bool:
        with self._lock:
            return terminal_id in self._held

    def defer(self, terminal_id: str, step: DeferredStep) -> bool:
        """Queue a step for the owner; false when no owner holds the id."""
        with self._lock:
            steps = self._held.get(terminal_id)
            if steps is None:
                return False
            steps.append(step)
            return True

    def release(self, terminal_id: str) -> list[DeferredStep]:
        """Drop the claim and hand back its deferred steps in one call."""
        with self._lock:
            return self._held.pop(terminal_id, [])


in_doubt_spawns = InDoubtRegistry()
