"""Bounded executor and cancellation boundary for worktree deletion."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable

from gobby.telemetry.instruments import observe_histogram
from gobby.threaded_executor import ManagedExecutorStats
from gobby.utils.git import run_to_completion


class DestructiveBoundary:
    """Coordinate cancellation with the first destructive mutation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._mutation_started = False

    def cancel_before_mutation(self) -> bool:
        """Request abandonment when mutation has not started."""
        with self._lock:
            if self._mutation_started:
                return False
            self._cancelled = True
            return True

    def begin_mutation(self) -> bool:
        """Enter the destructive phase unless cancellation won the race."""
        with self._lock:
            if self._cancelled:
                return False
            self._mutation_started = True
            return True

    @property
    def mutation_started(self) -> bool:
        with self._lock:
            return self._mutation_started


class WorktreeDeleteExecutor:
    """Bound asynchronous deletions and drain mutations before daemon shutdown."""

    def __init__(
        self,
        *,
        max_workers: int = 4,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.max_workers = max_workers
        self._slots = asyncio.Semaphore(max_workers)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queued: dict[asyncio.Task[object], float] = {}
        self._active: set[asyncio.Task[object]] = set()
        self._revoked: set[asyncio.Task[object]] = set()
        self._submitted = 0
        self._completed = 0
        self._cancelled = 0
        self._shutdown = False
        self._drained = threading.Event()
        self._drained.set()
        self._joined = threading.Event()

    async def run_delete[T](self, operation: Callable[[DestructiveBoundary], Awaitable[T]]) -> T:
        """Run one async delete, abandoning it only before its mutation boundary."""
        boundary = DestructiveBoundary()
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("WorktreeDeleteExecutor is shut down")
            if self._loop is not None and self._loop is not loop:
                raise RuntimeError("WorktreeDeleteExecutor belongs to another event loop")
            self._loop = loop
            task = loop.create_task(self._execute(operation, boundary))
            self._queued[task] = time.monotonic()
            self._submitted += 1
            self._drained.clear()
            task.add_done_callback(self._settle)

        def on_cancel() -> None:
            if boundary.cancel_before_mutation():
                task.cancel()

        return await run_to_completion(task, on_cancel=on_cancel)

    async def _execute[T](
        self,
        operation: Callable[[DestructiveBoundary], Awaitable[T]],
        boundary: DestructiveBoundary,
    ) -> T:
        task = asyncio.current_task()
        assert task is not None
        async with self._slots:
            with self._lock:
                if task in self._revoked:
                    raise asyncio.CancelledError
                queued_at = self._queued.pop(task)
                self._active.add(task)
            observe_histogram(
                "worktree_delete_executor_queue_wait_seconds", time.monotonic() - queued_at
            )
            try:
                return await operation(boundary)
            finally:
                with self._lock:
                    self._active.discard(task)

    def _settle(self, task: asyncio.Task[object]) -> None:
        with self._lock:
            self._queued.pop(task, None)
            self._revoked.discard(task)
            self._completed += 1
            self._cancelled += int(task.cancelled())
            if not self._queued and not self._active:
                self._drained.set()

    def stats(self) -> ManagedExecutorStats:
        with self._lock:
            return ManagedExecutorStats(
                max_workers=self.max_workers,
                active=len(self._active),
                queued=len(self._queued),
                submitted=self._submitted,
                completed=self._completed,
                cancelled=self._cancelled,
                threads=0,
                oldest_queue_seconds=max(
                    (time.monotonic() - queued_at for queued_at in self._queued.values()),
                    default=0.0,
                ),
                shutdown=self._shutdown,
            )

    def shutdown(self, *, cancel_futures: bool = True) -> None:
        """Stop admission and revoke queued operations, preserving active cleanup."""
        with self._lock:
            self._shutdown = True
            queued = list(self._queued) if cancel_futures else []
            self._revoked.update(queued)
        for task in queued:
            task.get_loop().call_soon_threadsafe(task.cancel)

    def join(self) -> None:
        """Wait off-loop while admitted operations finish on the daemon loop."""
        self.shutdown(cancel_futures=False)
        self._drained.wait()
        self._joined.set()

    def is_joined(self) -> bool:
        return self._joined.is_set()


async def run_worktree_delete[T](
    executor: WorktreeDeleteExecutor | None,
    operation: Callable[[DestructiveBoundary], Awaitable[T]],
) -> T:
    """Run deletion through the daemon executor or an isolated fallback thread."""
    if executor is not None:
        return await executor.run_delete(operation)

    boundary = DestructiveBoundary()

    def on_cancel() -> None:
        boundary.cancel_before_mutation()

    return await run_to_completion(operation(boundary), on_cancel=on_cancel)
