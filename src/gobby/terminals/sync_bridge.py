"""Sync-to-async write seam for hook threads."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass

from gobby.terminals.runtime import IndeterminateWrite, LoopMisuse, WriteOutcome
from gobby.terminals.write_coordinator import WriteCoordinator, WriteRequest

_TIMEOUT_FLOOR = 0.1
_TIMEOUT_CEILING = 30.0


def clamp_hook_timeout(seconds: float) -> float:
    """Bound hook write waits to the 2.4 contract."""
    return min(_TIMEOUT_CEILING, max(_TIMEOUT_FLOOR, seconds))


@dataclass
class _InFlight:
    task: asyncio.Task[WriteOutcome]
    terminal_id: str
    action_key: str


class TerminalEffectBridge:
    """Submit WriteCoordinator work onto the runner loop from hook threads."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        coordinator: WriteCoordinator,
        *,
        timeout_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        self._loop = loop
        self._coordinator = coordinator
        self._timeout = clamp_hook_timeout(timeout_seconds)
        self._shutdown_timeout = clamp_hook_timeout(shutdown_timeout_seconds)
        self._in_flight: list[_InFlight] = []
        self._in_flight_lock = threading.Lock()

    def run(self, request: WriteRequest) -> WriteOutcome | LoopMisuse:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            return LoopMisuse()
        marker = threading.Event()
        waiter_returned = threading.Event()
        future = asyncio.run_coroutine_threadsafe(
            self._run_on_loop(request, marker, waiter_returned),
            self._loop,
        )
        try:
            return future.result(timeout=self._timeout)
        except FutureTimeoutError:
            waiter_returned.set()
            if marker.is_set():
                return IndeterminateWrite(detail="hook write timed out after dispatch")
            future.cancel()
            return IndeterminateWrite(detail="hook write cancelled before dispatch")
        except asyncio.CancelledError:
            if marker.is_set():
                return IndeterminateWrite(detail="hook write cancelled after dispatch")
            return IndeterminateWrite(detail="hook write cancelled before dispatch")

    async def _run_on_loop(
        self,
        request: WriteRequest,
        marker: threading.Event,
        waiter_returned: threading.Event,
    ) -> WriteOutcome:
        async def marked_write() -> WriteOutcome:
            outcome = await self._coordinator.write(request, on_dispatch=marker.set)
            if waiter_returned.is_set() and request.origin == "automatic":
                self._coordinator.retain_unresolved(
                    request.terminal_id,
                    request.action_key,
                    request.origin,
                )
            return outcome

        task = asyncio.create_task(marked_write())
        item = _InFlight(task=task, terminal_id=request.terminal_id, action_key=request.action_key)
        with self._in_flight_lock:
            self._in_flight.append(item)
        try:
            return await asyncio.shield(task)
        finally:
            with self._in_flight_lock:
                if item in self._in_flight:
                    self._in_flight.remove(item)

    async def drain(self, timeout_seconds: float | None = None) -> None:
        timeout = clamp_hook_timeout(
            self._shutdown_timeout if timeout_seconds is None else timeout_seconds
        )
        with self._in_flight_lock:
            pending = list(self._in_flight)
        if not pending:
            return
        done, unfinished = await asyncio.wait(
            [item.task for item in pending],
            timeout=timeout,
        )
        del done
        for item in pending:
            if item.task not in unfinished:
                continue
            self._coordinator.quarantine(item.terminal_id, item.action_key)
