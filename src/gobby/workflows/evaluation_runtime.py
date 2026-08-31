"""Isolated runtime for synchronous workflow hook evaluation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

DEFAULT_WORKFLOW_BLOCKING_WORKERS = 8
_SHUTDOWN_JOIN_TIMEOUT_SECONDS = 1.0
_TASK_CANCELLATION_TIMEOUT_SECONDS = 0.1

T = TypeVar("T")


class WorkflowEvaluationRuntime:
    """Run workflow coroutines away from the daemon event loop."""

    def __init__(self, max_workers: int = DEFAULT_WORKFLOW_BLOCKING_WORKERS) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")

        self._max_workers = max_workers
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._closing = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="gobby-workflow-runtime",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

        if self._startup_error is not None:
            raise RuntimeError(
                "Workflow evaluation runtime failed to start"
            ) from self._startup_error
        if self._loop is None:
            raise RuntimeError("Workflow evaluation runtime failed to initialize")

    def run(self, coroutine: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
        """Run a coroutine on the isolated loop and return its result.

        ``timeout`` bounds the caller's wait, not the coroutine, which owns its
        own deadline. Callers are hook adapter threads from a small fixed pool,
        and a wedged loop would otherwise pin one past every deadline above it —
        the route's own ``wait_for`` cannot free it, because an executor future
        that has already started is not cancellable. On expiry the submitted
        coroutine is cancelled and ``TimeoutError`` propagates.
        """
        with self._lock:
            loop = self._loop
            if self._closing or loop is None or not loop.is_running():
                coroutine.close()
                raise RuntimeError("Workflow evaluation runtime is not running")

            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except BaseException:
                coroutine.close()
                raise

        try:
            return future.result(timeout)
        except TimeoutError:
            # A completed future means the coroutine raised its own timeout;
            # only an unfinished one is the wedge this bound exists to escape.
            if not future.done():
                future.cancel()
            raise

    @property
    def is_closing(self) -> bool:
        """Return whether controlled shutdown has started."""
        with self._lock:
            return self._closing

    def shutdown(self) -> None:
        """Stop accepting work and shut down without waiting for stalled helpers."""
        with self._lock:
            if self._closing:
                return
            self._closing = True
            loop = self._loop

        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            logger.warning("Workflow evaluation runtime did not stop within shutdown timeout")

    def _run_loop(self) -> None:
        loop: asyncio.AbstractEventLoop | None = None
        executor: concurrent.futures.ThreadPoolExecutor | None = None
        try:
            loop = asyncio.new_event_loop()
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="gobby-workflow-blocking",
            )
            loop.set_default_executor(executor)
            asyncio.set_event_loop(loop)
            with self._lock:
                self._loop = loop
            # Signal readiness from inside the running loop so __init__ cannot
            # return while run() would still see loop.is_running() as False.
            loop.call_soon(self._ready.set)
            loop.run_forever()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            logger.exception("Workflow evaluation runtime stopped unexpectedly")
        finally:
            if loop is not None:
                self._cancel_pending_tasks(loop)
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            if loop is not None:
                loop.close()
            asyncio.set_event_loop(None)

    @staticmethod
    def _cancel_pending_tasks(loop: asyncio.AbstractEventLoop) -> None:
        pending = asyncio.all_tasks(loop)
        if not pending:
            return

        for task in pending:
            task.cancel()
        _, still_pending = loop.run_until_complete(
            asyncio.wait(pending, timeout=_TASK_CANCELLATION_TIMEOUT_SECONDS)
        )
        if still_pending:
            logger.warning(
                "Workflow evaluation runtime closed with %d pending task(s)",
                len(still_pending),
            )
