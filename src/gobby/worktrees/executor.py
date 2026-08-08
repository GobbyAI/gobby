"""Bounded executor and cancellation boundary for worktree deletion."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

from gobby.threaded_executor import ManagedThreadPoolExecutor
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


class WorktreeDeleteExecutor(ManagedThreadPoolExecutor):
    """Daemon-owned bounded executor for complete worktree deletions."""

    def __init__(
        self,
        *,
        max_workers: int = 4,
        thread_name_prefix: str = "gobby-worktree-delete",
    ) -> None:
        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            queue_wait_metric="worktree_delete_executor_queue_wait_seconds",
            executor_name="WorktreeDeleteExecutor",
        )

    async def run_delete[T](self, operation: Callable[[DestructiveBoundary], T]) -> T:
        """Run one delete, abandoning it only before its mutation boundary."""
        boundary = DestructiveBoundary()
        future = self.submit(operation, boundary)

        def on_cancel() -> None:
            if boundary.cancel_before_mutation():
                future.cancel()

        return await run_to_completion(asyncio.wrap_future(future), on_cancel=on_cancel)


async def run_worktree_delete[T](
    executor: WorktreeDeleteExecutor | None,
    operation: Callable[[DestructiveBoundary], T],
) -> T:
    """Run deletion through the daemon executor or an isolated fallback thread."""
    if executor is not None:
        return await executor.run_delete(operation)

    boundary = DestructiveBoundary()

    def on_cancel() -> None:
        boundary.cancel_before_mutation()

    return await run_to_completion(
        asyncio.to_thread(operation, boundary),
        on_cancel=on_cancel,
    )
