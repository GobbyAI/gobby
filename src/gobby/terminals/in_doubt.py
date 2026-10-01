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
from datetime import datetime

DeferredStep = Callable[[], Awaitable[object]]
Attempt = tuple[int, datetime]


class InDoubtRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Each step carries whether it also runs when the owner keeps an orphan.
        self._held: dict[str, list[tuple[DeferredStep, bool]]] = {}
        # Held ids whose owner stopped unsettled, keyed to the attempt it was settling.
        self._suspended: dict[str, Attempt] = {}

    def claim(self, terminal_id: str, *, attempt: Attempt | None = None) -> bool:
        """Take the id for one owner; false, with nothing changed, when it is held.

        A suspended claim resumes, with its deferred steps, only for ``attempt``
        equal to the attempt it was suspended on.
        """
        with self._lock:
            if terminal_id in self._held:
                if attempt is None or self._suspended.get(terminal_id) != attempt:
                    return False
                del self._suspended[terminal_id]
                return True
            self._held[terminal_id] = []
            return True

    def holds(self, terminal_id: str) -> bool:
        with self._lock:
            return terminal_id in self._held

    def defer(self, terminal_id: str, step: DeferredStep, *, on_orphan: bool = False) -> bool:
        """Queue a step for the owner; false when no owner holds the id.

        A step runs after a proven exit, and also after a kept orphan when
        ``on_orphan`` is true.
        """
        with self._lock:
            steps = self._held.get(terminal_id)
            if steps is None:
                return False
            steps.append((step, on_orphan))
            return True

    def release(self, terminal_id: str, *, proven: bool = True) -> list[DeferredStep]:
        """Drop the claim and hand back the deferred steps its settlement runs."""
        with self._lock:
            self._suspended.pop(terminal_id, None)
            steps = self._held.pop(terminal_id, [])
        return [step for step, on_orphan in steps if proven or on_orphan]

    def suspend(self, terminal_id: str, attempt: Attempt) -> None:
        """End an unsettled claim without settling it.

        A claim with deferred steps stays held, so no other owner settles the row
        and loses them, until a claim on ``attempt`` resumes it. A claim with none
        is released.
        """
        with self._lock:
            if self._held.get(terminal_id):
                self._suspended[terminal_id] = attempt
            else:
                self._held.pop(terminal_id, None)


in_doubt_spawns = InDoubtRegistry()
