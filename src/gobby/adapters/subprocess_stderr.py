"""Helpers for continuously draining subprocess stderr pipes."""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import IO

from gobby.utils.stream_pump import open_stream_pump_executor

DEFAULT_STDERR_RING_LIMIT_BYTES = 64 * 1024
DEFAULT_STDERR_LOG_LIMIT_CHARS = 300


def compact_stderr(data: bytes | str | None, *, limit: int = 300) -> str | None:
    """Return a compact, printable stderr tail."""
    if not data:
        return None
    if isinstance(data, bytes):
        text = data.decode(errors="replace")
    else:
        text = data
    text = text.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"...{text[-limit:]}"


class StderrRingBuffer:
    """Bounded byte buffer retaining the most recent stderr output."""

    def __init__(self, limit: int = DEFAULT_STDERR_RING_LIMIT_BYTES) -> None:
        self._limit = limit
        self._data = bytearray()

    def append(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode(errors="replace")
        if len(data) >= self._limit:
            self._data = bytearray(data[-self._limit :])
            return
        self._data.extend(data)
        overflow = len(self._data) - self._limit
        if overflow > 0:
            del self._data[:overflow]

    def snapshot(self) -> bytes:
        return bytes(self._data)

    def compact_text(self, *, limit: int = DEFAULT_STDERR_LOG_LIMIT_CHARS) -> str | None:
        return compact_stderr(self.snapshot(), limit=limit)


class SubprocessStderrDrain:
    """Background stderr pipe drain backed by a bounded ring buffer."""

    def __init__(
        self,
        label: str,
        *,
        logger: logging.Logger | None = None,
        limit: int = DEFAULT_STDERR_RING_LIMIT_BYTES,
        log_limit: int = DEFAULT_STDERR_LOG_LIMIT_CHARS,
    ) -> None:
        self._label = label
        self._logger = logger
        self._log_limit = log_limit
        self._buffer = StderrRingBuffer(limit)
        self._task: asyncio.Task[None] | None = None
        self._updated = asyncio.Event()
        self._pump_executor: ThreadPoolExecutor | None = None

    def start_text(self, stream: IO[str] | None) -> None:
        """Start draining a synchronous text-mode stderr stream.

        A child that never writes to stderr leaves the read blocked for its
        whole life, so the drain takes a thread of its own instead of one of
        the loop's shared default-executor slots (#20839).
        """
        if stream is None:
            return
        self._shutdown_pump_executor()
        executor = open_stream_pump_executor(self._label)
        self._pump_executor = executor
        self._replace_task(asyncio.create_task(self._drain_text(stream, executor)))

    def start_async(self, stream: asyncio.StreamReader | None) -> None:
        """Start draining an asyncio subprocess stderr stream."""
        if stream is None:
            return
        self._replace_task(asyncio.create_task(self._drain_async(stream)))

    async def stop(self) -> None:
        """Cancel the active drain task, if any."""
        task = self._task
        self._task = None
        try:
            if task is None or task.done():
                return
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            self._shutdown_pump_executor()

    def _shutdown_pump_executor(self) -> None:
        """Release the pump thread; it exits once its stream reaches EOF."""
        executor = self._pump_executor
        self._pump_executor = None
        if executor is not None:
            executor.shutdown(wait=False)

    async def wait_finished(self, *, timeout: float = 0.1) -> None:
        """Wait briefly for the drain task to consume EOF after process exit."""
        task = self._task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            return

    async def wait_for_text(self, text: str, *, timeout: float = 1.0) -> bool:
        """Wait until drained stderr contains text, returning false on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if text in self.snapshot().decode(errors="replace"):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._updated.wait(), timeout=remaining)
            except TimeoutError:
                return False
            self._updated.clear()

    def snapshot(self) -> bytes:
        return self._buffer.snapshot()

    def compact_text(self, *, limit: int = DEFAULT_STDERR_LOG_LIMIT_CHARS) -> str | None:
        return self._buffer.compact_text(limit=limit)

    def _replace_task(self, task: asyncio.Task[None]) -> None:
        old_task = self._task
        if old_task is not None and not old_task.done():
            old_task.cancel()
        self._task = task

    async def _drain_text(self, stream: IO[str], executor: ThreadPoolExecutor) -> None:
        loop = asyncio.get_running_loop()
        fd = stream.fileno()
        while True:
            try:
                chunk = await loop.run_in_executor(executor, os.read, fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            self._capture(chunk)

    async def _drain_async(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            self._capture(chunk)

    def _capture(self, chunk: bytes | str) -> None:
        self._buffer.append(chunk)
        self._updated.set()
        if self._logger is None:
            return
        text = compact_stderr(chunk, limit=self._log_limit)
        if text:
            self._logger.debug("%s stderr: %s", self._label, text)
