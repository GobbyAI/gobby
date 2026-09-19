"""Per-terminal write latch, lock, lease revalidation, and sequences."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from gobby.storage.terminals import Terminal, UnresolvedWriteCapacityError
from gobby.terminals.host_client import HostCommandError
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.native_runtime import (
    NativeBatchFailure,
    NativeBatchOperation,
    NativeBatchResult,
    NativeBatchTarget,
    NativeTerminalRuntime,
)
from gobby.terminals.runtime import (
    AutomaticWriteQuarantined,
    Delivered,
    IndeterminateWrite,
    Suppressed,
    TerminalRuntime,
    TerminalRuntimeRegistry,
    TerminalWriteError,
    UnregisteredBackendError,
    WriteOutcome,
    is_named_key,
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
        daemon_epoch: str,
        at: datetime | None = None,
        payload_fingerprint: str | None = None,
    ) -> Terminal: ...

    def clear_unresolved_write(self, terminal_id: str, action_key: str) -> Terminal: ...

    def clear_all_unresolved_writes(self, terminal_id: str) -> Terminal: ...

    def set_automatic_write_quarantine(self, terminal_id: str, action_key: str) -> Terminal: ...

    def clear_automatic_write_quarantine(self, terminal_id: str) -> Terminal: ...


@dataclass(frozen=True)
class WriteRequest:
    """One coordinator-owned write identity."""

    terminal_id: str
    action_key: str
    origin: Literal["operator", "automatic", "attention", "daemon"]
    kind: Literal["text", "key", "paste", "input"]
    payload: str
    submit: bool = False
    attachment_id: str | None = None
    expected_lease_generation: int | None = None
    idempotency_key: str | None = None
    # A tmux attachment served by an attach client: raw ``input`` goes into
    # that client's PTY so tmux, not the pane's program, interprets mouse
    # reports and key bindings. ``send-keys`` to the pane cannot carry those.
    client_fd: int | None = None
    # The row the attachment was granted against. With it present the
    # coordinator dispatches without reading ``terminals``, which keeps an
    # operator keystroke off Postgres entirely.
    terminal: Terminal | None = None


@dataclass(frozen=True)
class SequenceDelay:
    """Inter-step delay held under the per-terminal lock."""

    seconds: float


@dataclass(frozen=True)
class NativeWakeBatchRequest:
    """One native terminal's complete composer-clear and wake action."""

    result_id: str
    terminal_id: str
    clear_action_key: str
    wake_action_key: str
    operations: tuple[NativeBatchOperation, ...]


class StaleTerminalLeaseError(RuntimeError):
    """Operator write/resize lost the lease between enqueue and dispatch."""


class RuntimeUnavailableError(TerminalWriteError):
    """The terminal row names a backend with no registered runtime."""

    def __init__(self, backend: str) -> None:
        super().__init__(stage="none")
        self.backend = backend


class IdempotencyConflictError(RuntimeError):
    """One idempotency key was reused for a different terminal payload."""

    code = "idempotency_conflict"


class WriteCoordinator:
    """Serializes writes, latches action_key, and revalidates leases."""

    def __init__(
        self,
        store: UnresolvedWriteStore,
        registry: TerminalRuntimeRegistry,
        *,
        lease_registry: TerminalLeaseRegistry,
    ) -> None:
        self._store = store
        self._registry = registry
        self.lease_registry = lease_registry
        self._daemon_epoch = lease_registry.daemon_epoch
        self._attention_gate: Callable[[Terminal], Awaitable[None]] | None = None
        # Terminals quarantined through this coordinator since their row was
        # last read; lets an operator write lift a quarantine set after its
        # attachment snapshot was taken without re-reading the row.
        self._quarantined: set[str] = set()

    def runtime_for(self, terminal: Terminal) -> TerminalRuntime:
        return self._registry.resolve(terminal.backend)

    def set_attention_gate(self, gate: Callable[[Terminal], Awaitable[None]]) -> None:
        self._attention_gate = gate

    def lock_held(self, terminal_id: str) -> bool:
        return self.lease_registry.lock_held(terminal_id)

    async def write(
        self,
        request: WriteRequest,
        *,
        on_dispatch: Callable[[], None] | None = None,
    ) -> WriteOutcome:
        async with self.lease_registry.lock(request.terminal_id):
            payload_fingerprint = (
                _payload_fingerprint(request) if request.idempotency_key is not None else None
            )
            if payload_fingerprint is not None:
                replay = self._idempotent_replay(request, payload_fingerprint)
                if replay is not None:
                    return replay
            blocked = self._blocked_automatic(
                request.terminal_id, request.action_key, request.origin
            )
            if blocked is not None:
                return blocked
            return await self._write_locked(
                request,
                latch=_latches(request),
                on_dispatch=on_dispatch,
                payload_fingerprint=payload_fingerprint,
            )

    def observe_resolved(self, terminal_id: str, action_key: str) -> None:
        """Positive observation of one logical action; clears only that key."""
        self._clear(terminal_id, action_key)
        terminal = self._store.get(terminal_id)
        if terminal is not None and terminal.automatic_write_quarantine_action_key == action_key:
            self._quarantined.discard(terminal_id)
            self._store.clear_automatic_write_quarantine(terminal_id)

    async def clear_on_exit(self, terminal_id: str) -> None:
        """Terminal exit clears every unresolved key and the quarantine pair."""
        async with self.lease_registry.lock(terminal_id):
            self._store.clear_all_unresolved_writes(terminal_id)
            self._quarantined.discard(terminal_id)
            self._store.clear_automatic_write_quarantine(terminal_id)

    def quarantine(self, terminal_id: str, action_key: str) -> None:
        self._quarantined.add(terminal_id)
        self._store.set_automatic_write_quarantine(terminal_id, action_key)

    def retain_unresolved(self, terminal_id: str, action_key: str, origin: str) -> None:
        """Keep an action latched after a late Delivered settlement."""
        self._persist(terminal_id, action_key, origin)

    async def run_sequence(
        self,
        terminal_id: str,
        *,
        action_key: str,
        origin: Literal["operator", "automatic", "attention"],
        steps: Sequence[WriteRequest | SequenceDelay],
        attachment_id: str | None = None,
        expected_lease_generation: int | None = None,
        latch: bool = True,
    ) -> WriteOutcome:
        """Write one logical action as an ordered sequence under the terminal lock.

        ``latch=False`` skips the write-ahead latch for an action whose steps
        are idempotent on the terminal, such as a composer drain: a lost reply
        then leaves no ``unresolved_writes`` entry to suppress the next attempt,
        because repeating the action cannot double-write anything. Quarantine
        still applies to unlatched automatic actions.
        """
        async with self.lease_registry.lock(terminal_id):
            blocked = self._blocked_automatic(terminal_id, action_key, origin)
            if blocked is not None:
                return blocked
            dispatched = False
            in_flight: asyncio.Task[WriteOutcome] | None = None
            try:
                if latch:
                    self._revalidate_lease(
                        terminal_id,
                        origin=origin,
                        attachment_id=attachment_id,
                        expected_generation=expected_lease_generation,
                    )
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
                    in_flight = asyncio.create_task(self._dispatch(step))
                    outcome = await in_flight
                    in_flight = None
                    dispatched = True
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

    async def run_native_wake_batch(
        self,
        requests: Sequence[NativeWakeBatchRequest],
    ) -> list[NativeBatchResult]:
        """Latch and atomically order one bounded native-host wake batch."""
        results: dict[str, NativeBatchResult] = {}
        duplicate_terminals = {
            terminal_id
            for terminal_id in {request.terminal_id for request in requests}
            if sum(request.terminal_id == terminal_id for request in requests) > 1
        }
        lock_ids = sorted({request.terminal_id for request in requests} - duplicate_terminals)
        async with AsyncExitStack() as stack:
            for terminal_id in lock_ids:
                await stack.enter_async_context(self.lease_registry.lock(terminal_id))

            runtime: NativeTerminalRuntime | None = None
            prepared: list[NativeBatchTarget] = []
            had_wake_latch: dict[str, bool] = {}
            for request in requests:
                if request.terminal_id in duplicate_terminals:
                    results[request.result_id] = NativeBatchResult(
                        request.result_id,
                        NativeBatchFailure(
                            stage="none",
                            code="duplicate_terminal",
                            detail="native wake batch targets must be grouped by terminal",
                        ),
                    )
                    continue
                try:
                    terminal = self._require(request.terminal_id)
                    blocked = self._blocked_automatic(
                        request.terminal_id,
                        request.clear_action_key,
                        "automatic",
                    )
                except KeyError:
                    results[request.result_id] = NativeBatchResult(
                        request.result_id,
                        NativeBatchFailure(
                            stage="none",
                            code="terminal_not_found",
                            detail=f"terminal {request.terminal_id} was not found",
                        ),
                    )
                    continue
                if blocked is not None:
                    results[request.result_id] = NativeBatchResult(request.result_id, blocked)
                    continue
                try:
                    candidate_runtime = self.runtime_for(terminal)
                except UnregisteredBackendError as exc:
                    results[request.result_id] = NativeBatchResult(
                        request.result_id,
                        NativeBatchFailure(
                            stage="none", code="backend_unregistered", detail=str(exc)
                        ),
                    )
                    continue
                if not isinstance(candidate_runtime, NativeTerminalRuntime):
                    results[request.result_id] = NativeBatchResult(
                        request.result_id,
                        NativeBatchFailure(
                            stage="none",
                            code="native_batch_unavailable",
                            detail="terminal runtime does not support native wake batching",
                        ),
                    )
                    continue
                if runtime is None:
                    runtime = candidate_runtime
                elif runtime is not candidate_runtime:
                    results[request.result_id] = NativeBatchResult(
                        request.result_id,
                        NativeBatchFailure(
                            stage="none",
                            code="native_batch_unavailable",
                            detail="native wake targets do not share one host connection",
                        ),
                    )
                    continue

                had_latch = request.wake_action_key in terminal.unresolved_writes
                had_wake_latch[request.result_id] = had_latch
                if not had_latch:
                    try:
                        self._persist(request.terminal_id, request.wake_action_key, "automatic")
                    except UnresolvedWriteCapacityError as exc:
                        results[request.result_id] = NativeBatchResult(
                            request.result_id,
                            NativeBatchFailure(
                                stage="none",
                                code="unresolved_write_capacity",
                                detail=str(exc),
                            ),
                        )
                        continue
                prepared.append(
                    NativeBatchTarget(
                        result_id=request.result_id,
                        terminal=terminal,
                        operations=request.operations,
                    )
                )

            if prepared and runtime is not None:
                in_flight = asyncio.create_task(runtime.write_batch(prepared))
                try:
                    batch_results = await in_flight
                except asyncio.CancelledError:
                    await asyncio.shield(in_flight)
                    raise
                except Exception as exc:
                    batch_results = [
                        NativeBatchResult(target.result_id, IndeterminateWrite(detail=str(exc)))
                        for target in prepared
                    ]
                request_by_result = {request.result_id: request for request in requests}
                for result in batch_results:
                    request = request_by_result[result.result_id]
                    if isinstance(result.outcome, Delivered):
                        self._clear(request.terminal_id, request.wake_action_key)
                    elif (
                        isinstance(result.outcome, NativeBatchFailure)
                        and result.outcome.stage == "none"
                        and not had_wake_latch[result.result_id]
                    ):
                        self._clear(request.terminal_id, request.wake_action_key)
                    results[result.result_id] = result

        return [results[request.result_id] for request in requests]

    async def _write_locked(
        self,
        request: WriteRequest,
        *,
        latch: bool,
        on_dispatch: Callable[[], None] | None = None,
        payload_fingerprint: str | None = None,
    ) -> WriteOutcome:
        terminal = request.terminal
        if terminal is None:
            terminal = self._require(request.terminal_id)
        if request.origin == "attention" and self._attention_gate is not None:
            await self._attention_gate(terminal)
        self._revalidate_lease(
            request.terminal_id,
            origin=request.origin,
            attachment_id=request.attachment_id,
            expected_generation=request.expected_lease_generation,
        )
        if latch:
            self._persist(
                request.terminal_id,
                request.action_key,
                request.origin,
                payload_fingerprint=payload_fingerprint,
            )
        if on_dispatch is not None:
            on_dispatch()
        try:
            outcome = await self._dispatch(request, terminal)
        except TerminalWriteError as exc:
            if latch and exc.stage == "none":
                self._clear(request.terminal_id, request.action_key)
            raise
        except Exception:
            raise
        if latch and not isinstance(outcome, IndeterminateWrite):
            self._clear(request.terminal_id, request.action_key)
        if isinstance(outcome, Delivered) and request.origin == "operator":
            self._release_quarantine(terminal)
        return outcome

    def _idempotent_replay(
        self,
        request: WriteRequest,
        payload_fingerprint: str,
    ) -> IndeterminateWrite | None:
        terminal = self._require(request.terminal_id)
        entry = terminal.unresolved_writes.get(request.action_key)
        if entry is None:
            return None
        stored_fingerprint = (
            entry.get("payload_fingerprint") if isinstance(entry, Mapping) else None
        )
        if stored_fingerprint != payload_fingerprint:
            raise IdempotencyConflictError(
                "idempotency key was already used with a different payload"
            )
        return IndeterminateWrite(detail="send_keys write outcome remains indeterminate")

    def _blocked_automatic(
        self,
        terminal_id: str,
        action_key: str,
        origin: str,
    ) -> WriteOutcome | None:
        if origin != "automatic":
            return None
        terminal = self._require(terminal_id)
        if (
            terminal.automatic_write_quarantined_at is not None
            and terminal.automatic_write_quarantine_action_key != action_key
        ):
            return AutomaticWriteQuarantined(action_key=action_key)
        if action_key in terminal.unresolved_writes:
            return Suppressed(action_key=action_key)
        return None

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
        if (
            attachment_id is None
            or expected_generation is None
            or self.lease_registry.holder(terminal_id) != attachment_id
            or self.lease_registry.generation(terminal_id) != expected_generation
        ):
            raise StaleTerminalLeaseError("lease is no longer current")

    def _persist(
        self,
        terminal_id: str,
        action_key: str,
        origin: str,
        *,
        payload_fingerprint: str | None = None,
    ) -> None:
        self._store.persist_unresolved_write(
            terminal_id,
            action_key,
            origin,
            daemon_epoch=self._daemon_epoch,
            payload_fingerprint=payload_fingerprint,
        )

    def _clear(self, terminal_id: str, action_key: str) -> None:
        self._store.clear_unresolved_write(terminal_id, action_key)

    def _require(self, terminal_id: str) -> Terminal:
        terminal = self._store.get(terminal_id)
        if terminal is None:
            raise KeyError(terminal_id)
        return terminal

    def observe_operator_input(self, terminal_id: str) -> None:
        """Lift the automatic-write quarantine after the host accepted direct input.

        gclient keystrokes never cross the daemon, so the host's ``input_activity``
        event is the only proof the operator typed. The row comes from the lease
        holder's attachment snapshot, the same row an operator write dispatches
        against, so a keystroke never re-reads the terminal; the store is consulted
        only when no holder snapshot exists.
        """
        terminal: Terminal | None = None
        holder = self.lease_registry.holder(terminal_id)
        if holder is not None:
            record = self.lease_registry.get(holder)
            if record is not None:
                terminal = record.terminal
        if terminal is None:
            terminal = self._store.get(terminal_id)
        if terminal is None:
            return
        self._release_quarantine(terminal)

    def _release_quarantine(self, terminal: Terminal) -> None:
        """Lift an automatic-write quarantine once the operator has typed.

        The UPDATE runs only when a quarantine is visible, either on the row
        this write dispatched against or set in-process since that row was
        read, so a burst of keystrokes costs one round trip per quarantine
        episode rather than one per key.
        """
        if terminal.automatic_write_quarantined_at is None and terminal.id not in self._quarantined:
            return
        self._quarantined.discard(terminal.id)
        terminal.automatic_write_quarantined_at = None
        terminal.automatic_write_quarantine_action_key = None
        self._store.clear_automatic_write_quarantine(terminal.id)

    async def _dispatch(
        self,
        request: WriteRequest,
        terminal: Terminal | None = None,
    ) -> WriteOutcome:
        if terminal is None:
            terminal = self._require(request.terminal_id)
        try:
            runtime = self.runtime_for(terminal)
        except UnregisteredBackendError as exc:
            # Nothing was dispatched, so no bytes can exist — that is exactly
            # stage="none", which _write_locked clears the latch for. Letting the
            # KeyError subclass propagate would leave the latch persisted and
            # suppress every later automatic write to this terminal.
            raise RuntimeUnavailableError(terminal.backend) from exc
        try:
            if request.kind == "text":
                return await runtime.write_text(terminal, request.payload, request.submit)
            if request.kind == "key":
                if not is_named_key(request.payload):
                    raise TerminalWriteError(stage="none")
                return await runtime.write_key(terminal, request.payload)
            if request.kind == "input":
                data = request.payload.encode("utf-8")
                if request.client_fd is not None:
                    await asyncio.to_thread(_write_client_input, request.client_fd, data)
                    return Delivered()
                return await runtime.write_input(terminal, data)
            return await runtime.write_paste(terminal, request.payload)
        except HostCommandError as exc:
            # A typed host refusal is a write outcome, and the host reports the
            # stage it reached. Left unconverted it matches no caller's
            # TerminalWriteError arm: the latch stays persisted and suppresses
            # every later automatic write, and the WebSocket write handler dies
            # before answering the operator, so the keystroke is silently lost.
            if exc.stage == "partial":
                raise TerminalWriteError(stage="partial") from exc
            raise TerminalWriteError(stage="none") from exc


def _latches(request: WriteRequest) -> bool:
    """Whether one write needs the durable write-ahead latch.

    The latch keeps a write whose outcome was lost from being repeated blind:
    ``_blocked_automatic`` reads it for automatic keys and ``_idempotent_replay``
    for keyed sends. An operator write without an idempotency key has neither
    reader. Its replay guard is the in-memory ``client_write_seq`` ledger in the
    lease registry, and a daemon restart ends the attachment itself, so latching
    it would only add two Postgres round trips to every keystroke.
    """
    return request.origin != "operator" or request.idempotency_key is not None


def _write_client_input(fd: int, data: bytes) -> None:
    """Write every byte to the attach client's PTY, typing the failure by stage."""
    delivered = 0
    while delivered < len(data):
        try:
            delivered += os.write(fd, data[delivered:])
        except OSError as exc:
            raise TerminalWriteError(
                stage="partial" if delivered else "none",
                delivered_bytes=delivered,
            ) from exc


def _payload_fingerprint(request: WriteRequest) -> str:
    payload = json.dumps(
        {
            "kind": request.kind,
            "payload": request.payload,
            "submit": request.submit,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
