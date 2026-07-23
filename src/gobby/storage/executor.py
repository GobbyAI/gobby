"""Bounded executor for daemon-owned database work."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar, cast

T = TypeVar("T")


@dataclass(frozen=True)
class DatabaseExecutorStats:
    """Diagnostic snapshot for DatabaseExecutor."""

    max_workers: int
    active: int
    queued: int
    submitted: int
    completed: int
    threads: int
    shutdown: bool

    def as_dict(self) -> dict[str, int | bool]:
        """Return JSON-safe diagnostics."""
        return {
            "max_workers": self.max_workers,
            "active": self.active,
            "queued": self.queued,
            "submitted": self.submitted,
            "completed": self.completed,
            "threads": self.threads,
            "shutdown": self.shutdown,
        }


class DatabaseExecutor:
    """Bounded async bridge for blocking database storage calls."""

    def __init__(self, *, max_workers: int = 4, thread_name_prefix: str = "gobby-db") -> None:
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
        self._active = 0
        self._shutdown = False

    async def run(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run blocking database work on the bounded executor."""
        future = self.submit(func, *args, **kwargs)
        return cast(T, await asyncio.wrap_future(future))

    def submit(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> Future[T]:
        """Submit blocking database work for synchronous callers."""
        call = functools.partial(self._execute, func, *args, **kwargs)
        context = contextvars.copy_context()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("DatabaseExecutor is shut down")
            self._submitted += 1
            return self._executor.submit(context.run, call)

    def _execute(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        with self._lock:
            self._active += 1
        try:
            return func(*args, **kwargs)
        finally:
            with self._lock:
                self._active -= 1
                self._completed += 1

    def stats(self) -> DatabaseExecutorStats:
        """Return a best-effort diagnostic snapshot."""
        with self._lock:
            active = self._active
            submitted = self._submitted
            completed = self._completed
            shutdown = self._shutdown

        queued = max(0, submitted - completed - active)
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
            threads=threads,
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
        self._executor.shutdown(wait=True, cancel_futures=False)
