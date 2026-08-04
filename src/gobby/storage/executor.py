"""Bounded executor for daemon-owned database work."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from gobby.telemetry.instruments import observe_histogram

T = TypeVar("T")


@dataclass(frozen=True)
class DatabaseExecutorStats:
    """Diagnostic snapshot for DatabaseExecutor."""

    max_workers: int
    active: int
    queued: int
    submitted: int
    completed: int
    cancelled: int
    threads: int
    oldest_queue_seconds: float
    shutdown: bool

    def as_dict(self) -> dict[str, int | float | bool]:
        """Return JSON-safe diagnostics."""
        return {
            "max_workers": self.max_workers,
            "active": self.active,
            "queued": self.queued,
            "submitted": self.submitted,
            "completed": self.completed,
            "cancelled": self.cancelled,
            "threads": self.threads,
            "oldest_queue_seconds": self.oldest_queue_seconds,
            "shutdown": self.shutdown,
        }


class DatabaseExecutor:
    """Bounded async bridge for blocking database storage calls."""

    def __init__(self, *, max_workers: int, thread_name_prefix: str = "gobby-db") -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._thread_name_prefix = thread_name_prefix
        self._lock = threading.Lock()
        self._submitted = 0
        self._completed = 0
        self._cancelled = 0
        self._active = 0
        self._pending: dict[int, float] = {}
        self._next_submission = 0
        self._shutdown = False
        self._join_started = False

    async def run(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run blocking database work on the bounded executor."""
        future = self.submit(func, *args, **kwargs)
        return cast(T, await asyncio.wrap_future(future))

    def submit(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> Future[T]:
        """Submit blocking database work for synchronous callers."""
        context = contextvars.copy_context()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("DatabaseExecutor is shut down")
            submission_id = self._next_submission
            self._next_submission += 1
            self._submitted += 1
            self._pending[submission_id] = time.monotonic()
        call = functools.partial(self._execute, submission_id, func, *args, **kwargs)
        try:
            future = self._executor.submit(context.run, call)
        except BaseException:
            with self._lock:
                self._submitted -= 1
                self._pending.pop(submission_id, None)
            raise
        future.add_done_callback(lambda done: self._settle(submission_id, done))
        return future

    def _execute(
        self,
        submission_id: int,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        with self._lock:
            queued_at = self._pending.pop(submission_id)
            self._active += 1
        observe_histogram("database_executor_queue_wait_seconds", time.monotonic() - queued_at)
        try:
            return func(*args, **kwargs)
        finally:
            with self._lock:
                self._active -= 1

    def _settle(self, submission_id: int, future: Future[Any]) -> None:
        with self._lock:
            self._pending.pop(submission_id, None)
            if future.cancelled():
                self._cancelled += 1
            self._completed += 1

    def stats(self) -> DatabaseExecutorStats:
        """Return a best-effort diagnostic snapshot."""
        with self._lock:
            active = self._active
            submitted = self._submitted
            completed = self._completed
            cancelled = self._cancelled
            queued = len(self._pending)
            now = time.monotonic()
            oldest_queue_seconds = max(
                (now - queued_at for queued_at in self._pending.values()),
                default=0.0,
            )
            shutdown = self._shutdown

        thread_prefix = f"{self._thread_name_prefix}_"
        threads = sum(
            1 for thread in threading.enumerate() if thread.name.startswith(thread_prefix)
        )
        return DatabaseExecutorStats(
            max_workers=self.max_workers,
            active=active,
            queued=queued,
            submitted=submitted,
            completed=completed,
            cancelled=cancelled,
            threads=threads,
            oldest_queue_seconds=oldest_queue_seconds,
            shutdown=shutdown,
        )

    def shutdown(self, *, cancel_futures: bool = True) -> None:
        """Atomically stop admission and revoke queued work without blocking."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._executor.shutdown(wait=False, cancel_futures=cancel_futures)

    def join(self) -> None:
        """Wait for running work after shutdown; callers must keep this off-loop."""
        with self._lock:
            if self._join_started:
                return
            self._shutdown = True
            self._join_started = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def is_joined(self) -> bool:
        """Return whether one caller owns the blocking join settlement."""
        with self._lock:
            return self._join_started
