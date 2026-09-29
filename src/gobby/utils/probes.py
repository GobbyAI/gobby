"""Run independent blocking probes concurrently under one shared deadline."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResults[T]:
    values: dict[str, T]
    timed_out: tuple[str, ...]


class ProbeBatch[T]:
    """Start every probe on its own worker thread; collect what finished in time.

    ``deadline`` is an absolute ``time.monotonic()`` value shared across batches,
    or None to wait for every probe. A probe still running at the deadline keeps
    its own subprocess timeout; nothing waits on it, so the caller can report
    now while the interpreter joins it at exit.
    """

    def __init__(self, probes: Mapping[str, Callable[[], T]], *, deadline: float | None) -> None:
        self._deadline = deadline
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, len(probes)), thread_name_prefix="gobby-probe"
        )
        self._futures = {name: self._executor.submit(probe) for name, probe in probes.items()}

    def collect(self) -> ProbeResults[T]:
        timeout = None if self._deadline is None else max(0.0, self._deadline - time.monotonic())
        done, _ = wait(self._futures.values(), timeout=timeout)
        self._executor.shutdown(wait=False, cancel_futures=True)
        # A probe's own exception propagates, as it did when probes ran inline.
        values = {name: future.result() for name, future in self._futures.items() if future in done}
        timed_out = tuple(name for name in self._futures if name not in values)
        return ProbeResults(values=values, timed_out=timed_out)
