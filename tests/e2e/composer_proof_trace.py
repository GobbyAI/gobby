"""Test-only observation of the existing writer, with a post-dispatch stage barrier."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from gobby.events.live_wake import RETRYABLE_WAKE_SKIPS
from gobby.sessions.handoff import build_handoff_continue_prompt
from gobby.storage.terminals import Terminal
from gobby.terminals.runtime import WriteOutcome
from gobby.terminals.write_coordinator import WriteRequest
from tests.e2e.composer_proof import ProofRefused, ProofScope, Surface

WAKE_TEXT = "Message from Gobby daemon: New activity available."


def completion_envelope(
    surface: Surface, external_id: str, machine_id: str, raw: dict[str, Any]
) -> dict[str, Any]:
    """Relay an actual public MCP result; this is controller transport only."""
    result = raw.get("result")
    if (
        raw.get("success") is not True
        or not isinstance(result, dict)
        or result.get("handoff_staged") is not True
        or result.get("delivery_pending") is not True
        or result.get("session_id") != surface.session_id
        or type(result.get("clear_session")) is not bool
        or not isinstance(result.get("attempt_id"), str)
        or not result["attempt_id"].strip()
        or result.get("session_type") != "terminal"
        or result.get("cli") != surface.provider
    ):
        raise ProofRefused("public staged result mismatch")
    try:
        UUID(machine_id)
        UUID(external_id)
    except (ValueError, TypeError) as exc:
        raise ProofRefused("registered provider identity required") from exc
    return {
        "schema_version": 1,
        "enqueued_at": datetime.now(UTC).isoformat(),
        "critical": False,
        "response_capability": "SUPPORTED",
        "hook_type": "post-tool-use" if surface.provider == "claude" else "PostToolUse",
        "source": surface.provider,
        "input_data": {
            "session_id": external_id,
            "machine_id": machine_id,
            "project_id": surface.project_id,
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": "set_handoff",
                "arguments": {"clear_session": result["clear_session"]},
            },
            "tool_output": raw,
        },
    }


async def observe_acquire(
    lock: asyncio.Lock,
    acquire: Callable[[asyncio.Lock], Awaitable[bool]],
    exchange: Callable[[dict[str, object]], Awaitable[None]],
    fields: dict[str, object],
) -> bool:
    """Observe the original acquire in its original task, including its real waiter.

    The next loop callback runs after acquire has either returned or suspended on
    the original lock's waiter. No replacement lock, child acquire or timed sleep
    participates in the physical composer boundary.
    """
    loop = asyncio.get_running_loop()
    before = set(lock._waiters or ())
    queued: asyncio.Future[asyncio.Future[None] | None] = loop.create_future()
    settled = False
    acquired = False
    cancellation: asyncio.CancelledError | None = None

    def record_waiter() -> None:
        waiter = next((item for item in lock._waiters or () if item not in before), None)
        if not settled and lock.locked() and waiter is not None and not waiter.done():
            queued.set_result(asyncio.ensure_future(exchange({**fields, "state": "queued"})))
        else:
            queued.set_result(None)

    loop.call_soon(record_waiter)
    try:
        try:
            acquired = await acquire(lock)
        except asyncio.CancelledError as exc:
            cancellation = exc
        finally:
            settled = True

        async def finish_receipts() -> None:
            pending = await queued
            if pending is not None:
                await pending
            await exchange({**fields, "state": "cancelled" if cancellation else "acquired"})

        receipt = asyncio.create_task(finish_receipts())
        while True:
            try:
                await asyncio.shield(receipt)
                break
            except asyncio.CancelledError as exc:
                if receipt.done():
                    raise
                cancellation = cancellation or exc
        if cancellation is not None:
            raise cancellation
        return acquired
    except BaseException:
        if acquired:
            lock.release()
        raise


async def observe_dispatch(
    request: WriteRequest,
    terminal: Terminal | None,
    dispatch: Callable[[WriteRequest, Terminal | None], Awaitable[WriteOutcome]],
    exchange: Callable[[dict[str, object]], Awaitable[None]],
) -> WriteOutcome:
    event: dict[str, object] = {
        "terminal_id": request.terminal_id,
        "session_id": None if terminal is None else terminal.session_id,
        "project_id": None if terminal is None else terminal.project_id,
        "host_epoch": None if terminal is None else terminal.host_epoch,
        "origin": request.origin,
        "kind": request.kind,
        "submit": request.submit,
        "payload_sha256": hashlib.sha256(request.payload.encode()).hexdigest(),
        "action_sha256": hashlib.sha256(request.action_key.encode()).hexdigest(),
    }
    await exchange({**event, "phase": "before"})
    outcome = await dispatch(request, terminal)
    await exchange({**event, "phase": "after", "outcome": type(outcome).__name__})
    return outcome


class ProofTrace:
    """Private fixture socket; its reply gates an actual coordinator dispatch."""

    def __init__(self, scope: ProofScope, socket: Path) -> None:
        self.scope = scope
        self.socket = socket
        self.surfaces: dict[str, Surface] = {}
        self.clear_attempts: dict[str, str] = {}
        self.predecessors: dict[tuple[str, str], Surface] = {}
        self.authorize_clear: Callable[[Surface, Surface, str], Awaitable[None]] | None = None
        self.bindings = asyncio.Lock()
        self.events: list[dict[str, object]] = []
        self.changed = asyncio.Condition()
        self.staged = asyncio.Event()
        self.release = asyncio.Event()
        self.admitted = asyncio.Event()
        self.admission_release = asyncio.Event()
        self.admission_gate: tuple[str, str] | None = None
        self.acquired = asyncio.Event()
        self.acquisition_release = asyncio.Event()
        self.acquisition_gate: str | None = None
        self.gate: tuple[str, str] | None = None
        self.gate_phase = "after"
        self.server: asyncio.Server | None = None
        self.handlers: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._handle, path=self.socket)
        self.socket.chmod(0o600)

    def register(self, surface: Surface) -> None:
        self.scope.require_surface(surface, surface)
        if surface.terminal_id in self.surfaces:
            raise ProofRefused("proof terminal already registered")
        self.surfaces[surface.terminal_id] = surface

    def expect_clear(self, surface: Surface, attempt_id: str) -> None:
        current = self.surfaces.get(surface.terminal_id)
        if current is None:
            raise ProofRefused("unowned clear proof terminal")
        self.scope.require_surface(surface, current)
        try:
            UUID(attempt_id)
        except ValueError as exc:
            raise ProofRefused("invalid expected clear attempt") from exc
        if surface.terminal_id in self.clear_attempts:
            raise ProofRefused("clear attempt already expected")
        self.clear_attempts[surface.terminal_id] = attempt_id

    async def require_event_surface(self, event: dict[str, object], caller: bool) -> Surface:
        async with self.bindings:
            expected = self.surfaces.get(str(event.get("terminal_id")))
            if expected is None:
                raise ProofRefused("unowned proof terminal")
            observed = Surface(
                expected.provider,
                str(event.get("session_id")),
                str(event.get("terminal_id")),
                str(event.get("host_epoch")),
                str(event.get("project_id")),
            )
            self.scope.require_surface(observed, observed)
            if expected == observed:
                return expected
            historical = self.predecessors.get((observed.session_id, str(event.get("attempt_id"))))
            if caller and historical == observed:
                return observed
            attempt_id = self.clear_attempts.get(expected.terminal_id)
            if (
                caller
                or observed in self.predecessors.values()
                or attempt_id is None
                or self.authorize_clear is None
            ):
                raise ProofRefused("proof surface binding changed")
            await self.authorize_clear(expected, observed, attempt_id)
            self.predecessors[(expected.session_id, attempt_id)] = expected
            self.surfaces[expected.terminal_id] = observed
            del self.clear_attempts[expected.terminal_id]
            return observed

    def arm(self, surface: Surface, payload: str) -> None:
        self.scope.require_surface(surface, self.surfaces[surface.terminal_id])
        if payload not in {WAKE_TEXT, "/compact", "/clear"} or self.gate is not None:
            raise ProofRefused("invalid stage gate")
        transport = payload + "\n" if payload in {"/compact", "/clear"} else payload
        self.gate = (surface.terminal_id, hashlib.sha256(transport.encode()).hexdigest())
        self.gate_phase = "after"
        self.staged.clear()
        self.release.clear()

    def arm_before(self, surface: Surface, payload: str) -> None:
        self.arm(surface, payload)
        self.gate_phase = "before"

    def arm_admission(self, surface: Surface, writer: str) -> None:
        current = self.surfaces.get(surface.terminal_id)
        if current is None or writer != "wake" or self.admission_gate is not None:
            raise ProofRefused("invalid wake admission barrier")
        self.scope.require_surface(surface, current)
        self.admitted.clear()
        self.admission_release.clear()
        self.admission_gate = (surface.terminal_id, writer)

    def arm_wake_acquisition(self, surface: Surface) -> None:
        self.scope.require_surface(surface, self.surfaces[surface.terminal_id])
        if self.acquisition_gate is not None:
            raise ProofRefused("acquisition barrier is already armed")
        self.acquisition_gate = surface.terminal_id
        self.acquired.clear()
        self.acquisition_release.clear()

    async def accept(self, event: dict[str, object]) -> None:
        common = {"terminal_id", "session_id", "project_id", "host_epoch", "phase"}
        wake = event.get("phase") == "wake"
        lock = event.get("phase") == "lock"
        admission = event.get("phase") == "admission"
        caller = event.get("phase") == "caller"
        fields = common | (
            {"state", "attempt_id"}
            if caller
            else {"state", "writer", "lock_sha256", "attempt_id"}
            if lock or admission
            else {"delivered", "skipped", "priority", "monotonic", "requested_session_id"}
            if wake
            else {"origin", "kind", "submit", "payload_sha256", "action_sha256", "outcome"}
        )
        if set(event) - fields or fields - {"outcome"} - set(event):
            raise ProofRefused("invalid trace event fields")
        if event.get("phase") not in {"before", "after", "wake", "lock", "caller", "admission"}:
            raise ProofRefused("invalid trace phase")
        expected = await self.require_event_surface(event, caller)
        if caller:
            if (
                event.get("state") not in {"settled", "cancelled"}
                or not isinstance(event.get("attempt_id"), str)
                or not str(event["attempt_id"]).strip()
            ):
                raise ProofRefused("invalid actual caller receipt")
        elif lock or admission:
            if (
                event.get("state")
                not in ({"ready"} if admission else {"queued", "acquired", "cancelled"})
                or event.get("writer") not in {"wake", "handoff"}
                or not isinstance(event.get("lock_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(event["lock_sha256"]))
                or (event["writer"] == "wake" and event["attempt_id"] is not None)
                or (
                    event["writer"] == "handoff"
                    and (
                        not isinstance(event["attempt_id"], str)
                        or not str(event["attempt_id"]).strip()
                    )
                )
            ):
                raise ProofRefused("invalid lock receipt")
        elif wake:
            requested = Surface(
                expected.provider,
                str(event.get("requested_session_id")),
                expected.terminal_id,
                expected.host_epoch,
                expected.project_id,
            )
            self.scope.require_surface(requested, requested)
            if requested != expected and requested not in self.predecessors.values():
                raise ProofRefused("wake request has no canonical physical lineage")
            if (
                event.get("priority") not in {"normal", "urgent"}
                or type(event.get("delivered")) is not bool
                or not isinstance(event.get("monotonic"), (int, float))
                or event.get("skipped")
                not in {
                    None,
                    "session_active",
                    "debounced",
                    "handoff_delivery_pending",
                    "session_awaiting_handoff",
                    "session_expired",
                    "session_not_live",
                }
                | RETRYABLE_WAKE_SKIPS
            ):
                raise ProofRefused("invalid wake outcome event")
        elif event.get("origin") not in {"operator", "automatic", "attention", "daemon"}:
            raise ProofRefused("unknown write origin")
        if not wake and not lock and not admission and not caller and event["origin"] != "operator":
            continuation = build_handoff_continue_prompt()
            allowed = {
                hashlib.sha256(value.encode()).hexdigest()
                for value in (
                    WAKE_TEXT,
                    "/compact",
                    "/compact\n",
                    "/clear",
                    "/clear\n",
                    "enter",
                    continuation,
                    continuation + "\n",
                )
            }
            if event.get("payload_sha256") not in allowed:
                raise ProofRefused("unexpected automatic bytes")
        async with self.changed:
            self.events.append({**event, "sequence": len(self.events) + 1})
            self.changed.notify_all()
        if admission and self.admission_gate == (expected.terminal_id, event.get("writer")):
            self.admitted.set()
            await asyncio.wait_for(self.admission_release.wait(), 45)
            self.admission_gate = None
        if (
            lock
            and event.get("state") == "acquired"
            and event.get("writer") == "wake"
            and self.acquisition_gate == expected.terminal_id
        ):
            self.acquired.set()
            await asyncio.wait_for(self.acquisition_release.wait(), 45)
            self.acquisition_gate = None
        if (
            event.get("phase") == self.gate_phase
            and event.get("kind") == "text"
            and (self.gate_phase == "before" or event.get("outcome") == "Delivered")
            and self.gate == (expected.terminal_id, event.get("payload_sha256"))
        ):
            self.staged.set()
            await asyncio.wait_for(self.release.wait(), 45)
            self.gate = None

    async def wait_for(
        self, predicate: Callable[[dict[str, object]], bool], *, after: int, timeout: float
    ) -> dict[str, object]:
        async with asyncio.timeout(timeout), self.changed:
            await self.changed.wait_for(
                lambda: any(predicate(item) for item in self.events[after:])
            )
            return next(item for item in self.events[after:] if predicate(item))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.handlers.add(task)
        try:
            raw = await asyncio.wait_for(reader.readline(), 5)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ProofRefused("invalid trace event")
            await self.accept(cast(dict[str, object], value))
            writer.write(b'{"accepted":true}\n')
            await writer.drain()
        except (ProofRefused, ValueError, TimeoutError):
            writer.write(b'{"accepted":false}\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            if task is not None:
                self.handlers.discard(task)

    async def close(self) -> None:
        self.release.set()
        self.admission_release.set()
        self.acquisition_release.set()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        pending = tuple(self.handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.socket.unlink(missing_ok=True)


async def exchange_trace(socket: Path, event: dict[str, object]) -> None:
    reader, writer = await asyncio.open_unix_connection(socket)
    try:
        writer.write(json.dumps(event).encode() + b"\n")
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 50))
        if not isinstance(reply, dict) or reply.get("accepted") is not True:
            raise ProofRefused("proof trace refused dispatch")
    finally:
        writer.close()
        await writer.wait_closed()
