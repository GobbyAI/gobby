"""Sample the event loop thread's Python stack from outside that thread.

The loop-lag watchdog can say the loop stopped scheduling, and for a stall
caused by one coroutine it can name the task. It cannot explain a stall whose
cost is spread over a large volume of ordinary Python -- and that is the shape
of the daemon's worst stalls: across 35 seconds of a real 68-second block, the
loop thread's time went 10% to garbage collection, 9% idle in the selector, and
the rest scattered across dict operations, isinstance checks and attribute
lookups, with no single hot call to blame.

Neither external profiler answers that here. ``sample(1)`` collapses every
Python frame into ``_PyEval_EvalFrameDefault``, and py-spy refuses to run
without root on macOS. A thread inside the process can do it: CPython releases
the GIL every few milliseconds, so this thread still gets scheduled while the
loop thread is burning, and ``sys._current_frames()`` returns that thread's
real Python frames (#20841).

The cost is one short wake-up per interval on a thread of its own -- a dict
lookup and a frame walk -- so it stays off the loop entirely.
"""

from __future__ import annotations

import logging
import sys
import sysconfig
import threading
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 0.01
# Deep enough to cross asyncio's internals and starlette's middleware chain into
# application frames, bounded so one pathological recursion cannot walk forever.
MAX_STACK_DEPTH = 80
# Aggregating on the innermost frames keeps distinct callers separable while
# collapsing the identical outer scaffolding every request carries.
STACK_SIGNATURE_FRAMES = 12
# The innermost frames alone name the blocking primitive and hide who called it:
# a four-second stall reported as a psycopg pool checkout with the route, the
# handler and the query all cut off left nothing to fix. The literal outermost
# frames are no help either -- every stack starts with the same interpreter,
# uvicorn and asyncio scaffolding -- so keep the nearest frames that are ours
# (#20845).
STACK_CALLER_FRAMES = 8
_ELIDED = "..."
_STDLIB_PREFIX = sysconfig.get_paths()["stdlib"]


def _is_our_frame(filename: str) -> bool:
    """Tell repository code apart from interpreter and dependency scaffolding."""
    return (
        not filename.startswith("<")
        and "/site-packages/" not in filename
        and not filename.startswith(_STDLIB_PREFIX)
    )


class LoopStackSampler:
    """Collect the watched thread's Python stacks until asked for them."""

    def __init__(
        self,
        thread_id: int,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._thread_id = thread_id
        self._interval_seconds = interval_seconds
        self._stacks: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        thread = threading.Thread(
            target=self._run,
            name="gobby-loop-stack-sampler",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Signal the thread and wait for it, so no sample lands after this."""
        thread = self._thread
        self._thread = None
        self._stop.set()
        if thread is not None:
            thread.join(timeout=self._interval_seconds * 10 + 1.0)

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def interval_seconds(self) -> float:
        """The nominal sampling period, so a report can state its own coverage."""
        return self._interval_seconds

    def drain(self) -> list[tuple[str, int]]:
        """Return the stacks collected since the last drain, hottest first."""
        with self._lock:
            collected = self._stacks
            self._stacks = Counter()
        return collected.most_common()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            signature = self._capture()
            if signature is None:
                continue
            with self._lock:
                self._stacks[signature] += 1

    def _capture(self) -> str | None:
        try:
            frame = sys._current_frames().get(self._thread_id)
        except Exception:  # pragma: no cover - a diagnostic must never raise
            return None
        if frame is None:
            return None
        frames: list[str] = []
        ours: list[bool] = []
        depth = 0
        while frame is not None and depth < MAX_STACK_DEPTH:
            code = frame.f_code
            frames.append(f"{code.co_qualname}@{Path(code.co_filename).name}")
            ours.append(_is_our_frame(code.co_filename))
            frame = frame.f_back
            depth += 1
        if not frames:
            return None
        # frames[0] is innermost; present outermost-first so a stack reads as a
        # call chain.
        if len(frames) <= STACK_SIGNATURE_FRAMES:
            return " -> ".join(reversed(frames))
        inner = frames[:STACK_SIGNATURE_FRAMES]
        callers = [
            label
            for label, is_ours in zip(
                frames[STACK_SIGNATURE_FRAMES:],
                ours[STACK_SIGNATURE_FRAMES:],
                strict=True,
            )
            if is_ours
        ][:STACK_CALLER_FRAMES]
        parts = (
            [*reversed(callers), _ELIDED, *reversed(inner)] if callers else list(reversed(inner))
        )
        return " -> ".join(parts)


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "MAX_STACK_DEPTH",
    "STACK_SIGNATURE_FRAMES",
    "LoopStackSampler",
]
