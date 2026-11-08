"""Per-recall stage timings, collected through a context variable.

A recall opens a ``recording()`` scope; every stage it passes through (embedding,
search legs, FalkorDB round trips) adds its elapsed seconds to that scope's
``StageTimings``. Tasks spawned inside the scope (``asyncio.gather``) and thread
hops (``asyncio.to_thread``) inherit the context and write to the same object.
Outside a scope ``record`` does nothing, so storage and graph code can call it
unconditionally.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class StageTimings:
    """Inclusive stage durations and counts for one recall, including pending waits."""

    seconds: dict[str, float] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    pending: dict[object, tuple[str, float]] = field(default_factory=dict)

    def add(self, stage: str, seconds: float) -> None:
        self.seconds[stage] = self.seconds.get(stage, 0.0) + seconds
        self.calls[stage] = self.calls.get(stage, 0) + 1

    def render(self) -> str:
        """Compact ``stage=<ms>ms/<calls>`` list, sorted by stage name."""
        seconds = self.seconds.copy()
        calls = self.calls.copy()
        now = time.perf_counter()
        pending = list(self.pending.values())
        for stage, started in pending:
            seconds[stage] = seconds.get(stage, 0.0) + now - started
            calls[stage] = calls.get(stage, 0) + 1
        rendered = " ".join(
            f"{stage}={seconds[stage] * 1000:.0f}ms/{calls[stage]}" for stage in sorted(seconds)
        )
        if pending:
            rendered += " active=" + ",".join(sorted({stage for stage, _started in pending}))
        return rendered


_current: contextvars.ContextVar[StageTimings | None] = contextvars.ContextVar(
    "memory_recall_stage_timings", default=None
)


@contextmanager
def recording(timings: StageTimings | None = None) -> Iterator[StageTimings]:
    """Open a recording scope for the current recall and yield its timings."""
    timings = timings if timings is not None else (_current.get() or StageTimings())
    token = _current.set(timings)
    try:
        yield timings
    finally:
        _current.reset(token)


def record(stage: str, seconds: float) -> None:
    """Add ``seconds`` to ``stage`` in the active recording, if there is one."""
    timings = _current.get()
    if timings is not None:
        timings.add(stage, seconds)


@contextmanager
def timed(stage: str) -> Iterator[None]:
    """Time the enclosed block into ``stage``, including when it raises."""
    start = time.perf_counter()
    timings = _current.get()
    marker = object()
    if timings is not None:
        timings.pending[marker] = (stage, start)
    try:
        yield
    finally:
        if timings is not None:
            timings.pending.pop(marker, None)
            timings.add(stage, time.perf_counter() - start)


async def timed_await[T](stage: str, awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` and record its elapsed time into ``stage``."""
    start = time.perf_counter()
    try:
        return await awaitable
    finally:
        record(stage, time.perf_counter() - start)
