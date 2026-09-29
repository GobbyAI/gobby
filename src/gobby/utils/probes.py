"""Run independent blocking probes concurrently under one shared deadline."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResults[T]:
    values: dict[str, T]
    timed_out: tuple[str, ...]


class ProbeBatch[T]:
    """Start every probe on its own daemon thread; collect what finished in time.

    ``deadline`` is an absolute ``time.monotonic()`` value shared across batches,
    or None to wait for every probe. A probe still running at the deadline is
    abandoned: its thread is a daemon, so the interpreter exits without joining it
    and a hung probe cannot hold the process past the deadline.
    """

    def __init__(self, probes: Mapping[str, Callable[[], T]], *, deadline: float | None) -> None:
        self._deadline = deadline
        self._values: dict[str, T] = {}
        self._errors: dict[str, Exception] = {}
        self._threads = {
            name: threading.Thread(
                target=self._run, args=(name, probe), name=f"gobby-probe-{name}", daemon=True
            )
            for name, probe in probes.items()
        }
        for thread in self._threads.values():
            thread.start()

    def _run(self, name: str, probe: Callable[[], T]) -> None:
        try:
            self._values[name] = probe()
        except Exception as exc:
            self._errors[name] = exc

    def collect(self) -> ProbeResults[T]:
        for thread in self._threads.values():
            timeout = (
                None if self._deadline is None else max(0.0, self._deadline - time.monotonic())
            )
            thread.join(timeout)
        finished = [name for name, thread in self._threads.items() if not thread.is_alive()]
        # A probe's own exception propagates, as it did when probes ran inline.
        for name in finished:
            if name in self._errors:
                raise self._errors[name]
        values = {name: self._values[name] for name in finished}
        timed_out = tuple(name for name in self._threads if name not in values)
        return ProbeResults(values=values, timed_out=timed_out)
