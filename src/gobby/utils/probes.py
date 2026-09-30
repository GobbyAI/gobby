"""Run independent blocking probes concurrently under one shared deadline."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Thread

from gobby.utils import spawn

# Time a probe past the deadline gets to reap the child spawn.run just killed.
REAP_GRACE_SECONDS = 0.5


@dataclass(frozen=True)
class ProbeResults[T]:
    values: dict[str, T]
    timed_out: tuple[str, ...]


class ProbeBatch[T]:
    """Start every probe on its own daemon thread; collect what finished in time.

    ``deadline`` is an absolute ``time.monotonic()`` value shared across batches,
    or None to wait for every probe. Each probe runs under
    ``spawn.thread_deadline``, so a subprocess it starts is killed and reaped at
    the deadline instead of outliving the caller. A probe still running after
    that (pure Python or network work, which owns no child) is abandoned on its
    daemon thread, so it cannot hold the interpreter open at exit.
    """

    def __init__(self, probes: Mapping[str, Callable[[], T]], *, deadline: float | None) -> None:
        self._deadline = deadline
        self._values: dict[str, T] = {}
        self._errors: dict[str, Exception] = {}
        self._finished_at: dict[str, float] = {}
        self._threads = {
            name: Thread(
                target=self._run, args=(name, probe), name=f"gobby-probe-{name}", daemon=True
            )
            for name, probe in probes.items()
        }
        for thread in self._threads.values():
            thread.start()

    def _run(self, name: str, probe: Callable[[], T]) -> None:
        scope = (
            contextlib.nullcontext()
            if self._deadline is None
            else spawn.thread_deadline(self._deadline)
        )
        try:
            with scope:
                self._values[name] = probe()
        except Exception as exc:
            self._errors[name] = exc
        finally:
            self._finished_at[name] = time.monotonic()

    def collect(self) -> ProbeResults[T]:
        # Anchored to the shared deadline, so batches that share it share one grace.
        join_until = None if self._deadline is None else self._deadline + REAP_GRACE_SECONDS
        for thread in self._threads.values():
            thread.join(None if join_until is None else max(0.0, join_until - time.monotonic()))
        # A probe that ended after the deadline was cut short by its capped spawn,
        # so its fallback value or TimeoutExpired is not a real answer.
        in_time = [
            name
            for name in self._threads
            if name in self._finished_at
            and (self._deadline is None or self._finished_at[name] <= self._deadline)
        ]
        # A probe's own exception propagates, as it did when probes ran inline.
        for name in in_time:
            if name in self._errors:
                raise self._errors[name]
        values = {name: self._values[name] for name in in_time}
        timed_out = tuple(name for name in self._threads if name not in values)
        return ProbeResults(values=values, timed_out=timed_out)
