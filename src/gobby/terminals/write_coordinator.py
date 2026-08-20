"""Per-terminal write latch, lock, lease revalidation, and sequences."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from gobby.storage.terminals import Terminal, UnresolvedWriteCapacityError
from gobby.terminals.runtime import (
    Delivered,
    IndeterminateWrite,
    TerminalRuntime,
    TerminalWriteError,
    WriteOutcome,
)


class UnresolvedWriteStore(Protocol):
    """Durable latch operations the coordinator requires."""

    def get(self, terminal_id: str) -> Terminal | None: ...

    def persist_unresolved_write(
        self,
        terminal_id: str,
        action_key: str,
        origin: str,
        *,
        at: datetime | None = None,
    ) -> Terminal: ...

    def clear_unresolved_write(self, terminal_id: str, action_key: str) -> Terminal: ...


@dataclass(frozen=True)
class WriteRequest:
    """One coordinator-owned write identity."""

    terminal_id: str
    action_key: str
    origin: Literal["operator", "automatic", "attention"]
    kind: Literal["text", "key", "paste"]
    payload: str
    submit: bool = False
    attachment_id: str | None = None
    expected_lease_generation: int | None = None


@dataclass(frozen=True)
class SequenceDelay:
    """Inter-step delay held under the per-terminal lock."""

    seconds: float


class StaleTerminalLeaseError(RuntimeError):
    """Operator write/resize lost the lease between enqueue and dispatch."""


@dataclass
class _Lease:
    attachment_id: str | None = None
    generation: int = 0


class WriteCoordinator:
    """Serializes writes, latches action_key, and revalidates leases."""

    def __init__(self, store: UnresolvedWriteStore, runtime: TerminalRuntime) -> None:
        self._store = store
        self._runtime = runtime
        self._locks: dict[str, asyncio.Lock] = {}
        self._leases: dict[str, _Lease] = {}
        self._attention_gate: Callable[[Terminal], Awaitable[None]] | None = None

    def set_attention_gate(self, gate: Callable[[Terminal], Awaitable[None]]) -> None:
        self._attention_gate = gate

    def lock_held(self, terminal_id: str) -> bool:
        return self._lock(terminal_id).locked()

    def _lock(self, terminal_id: str) -> asyncio.Lock:
        lock = self._locks.get(terminal_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[terminal_id] = lock
        return lock

    def _lease(self, terminal_id: str) -> _Lease:
        lease = self._leases.get(terminal_id)
        if lease is None:
            lease = _Lease()
            self._leases[terminal_id] = lease
        return lease

    async def grant_lease(self, terminal_id: str, attachment_id: str) -> int:
        async with self._lock(terminal_id):
            return self._grant_locked(terminal_id, attachment_id)

    async def takeover_lease(self, terminal_id: str, attachment_id: str) -> int:
        async with self._lock(terminal_id):
            return self._grant_locked(terminal_id, attachment_id)

    def _grant_locked(self, terminal_id: str, attachment_id: str) -> int:
        lease = self._lease(terminal_id)
        lease.generation += 1
        lease.attachment_id = attachment_id
        return lease.generation

    async def write(self, request: WriteRequest) -> WriteOutcome:
        async with self._lock(request.terminal_id):
            return await self._write_locked(request, latch=True)

    async def run_sequence(
        self,
        terminal_id: str,
        *,
        action_key: str,
        origin: Literal["operator", "automatic", "attention"],
        steps: Sequence[WriteRequest | SequenceDelay],
        attachment_id: str | None = None,
        expected_lease_generation: int | None = None,
    ) -> WriteOutcome:
        lock = self._lock(terminal_id)
        async with lock:
            dispatched = False
            in_flight: asyncio.Task[WriteOutcome] | None = None
            try:
                self._persist(terminal_id, action_key, origin)
                for step in steps:
                    if isinstance(step, SequenceDelay):
                        await asyncio.sleep(step.seconds)
                        continue
                    self._revalidate_lease(
                        terminal_id,
                        origin=origin,
                        attachment_id=attachment_id or step.attachment_id,
                        expected_generation=expected_lease_generation
                        if expected_lease_generation is not None
                        else step.expected_lease_generation,
                    )
                    dispatched = True
                    in_flight = asyncio.create_task(self._dispatch(step))
                    outcome = await in_flight
                    in_flight = None
                    if isinstance(outcome, IndeterminateWrite):
                        return outcome
                    if isinstance(outcome, Delivered):
                        continue
                self._clear(terminal_id, action_key)
                return Delivered()
            except asyncio.CancelledError:
                if in_flight is not None:
                    await asyncio.shield(in_flight)
                    dispatched = True
                if not dispatched:
                    self._clear(terminal_id, action_key)
                raise
            except UnresolvedWriteCapacityError:
                raise
            except StaleTerminalLeaseError:
                if not dispatched:
                    self._clear(terminal_id, action_key)
                raise
            except TerminalWriteError as exc:
                if exc.stage == "none" and not dispatched:
                    self._clear(terminal_id, action_key)
                raise

    async def _write_locked(self, request: WriteRequest, *, latch: bool) -> WriteOutcome:
        terminal = self._require(request.terminal_id)
        if request.origin == "attention" and self._attention_gate is not None:
            await self._attention_gate(terminal)
        if latch:
            self._persist(request.terminal_id, request.action_key, request.origin)
        self._revalidate_lease(
            request.terminal_id,
            origin=request.origin,
            attachment_id=request.attachment_id,
            expected_generation=request.expected_lease_generation,
        )
        try:
            outcome = await self._dispatch(request)
        except TerminalWriteError as exc:
            if exc.stage == "none":
                self._clear(request.terminal_id, request.action_key)
            raise
        except Exception:
            raise
        if isinstance(outcome, Delivered):
            self._clear(request.terminal_id, request.action_key)
        return outcome

    def _revalidate_lease(
        self,
        terminal_id: str,
        *,
        origin: str,
        attachment_id: str | None,
        expected_generation: int | None,
    ) -> None:
        if origin != "operator":
            return
        lease = self._lease(terminal_id)
        if (
            attachment_id is None
            or expected_generation is None
            or lease.attachment_id != attachment_id
            or lease.generation != expected_generation
        ):
            raise StaleTerminalLeaseError("lease is no longer current")

    def _persist(self, terminal_id: str, action_key: str, origin: str) -> None:
        self._store.persist_unresolved_write(terminal_id, action_key, origin)

    def _clear(self, terminal_id: str, action_key: str) -> None:
        self._store.clear_unresolved_write(terminal_id, action_key)

    def _require(self, terminal_id: str) -> Terminal:
        terminal = self._store.get(terminal_id)
        if terminal is None:
            raise KeyError(terminal_id)
        return terminal

    async def _dispatch(self, request: WriteRequest) -> WriteOutcome:
        terminal = self._require(request.terminal_id)
        if request.kind == "text":
            return await self._runtime.write_text(terminal, request.payload, request.submit)
        if request.kind == "key":
            return await self._runtime.write_key(terminal, request.payload)  # type: ignore[arg-type]
        return await self._runtime.write_paste(terminal, request.payload)
