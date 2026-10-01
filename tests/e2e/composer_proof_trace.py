"""Test-only observation of the existing writer, with a post-dispatch stage barrier."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from gobby.storage.terminals import Terminal
from gobby.terminals.runtime import WriteOutcome
from gobby.terminals.write_coordinator import WriteRequest
from tests.e2e.composer_proof import ProofRefused, ProofScope, Surface

WAKE_TEXT = "Message from Gobby daemon: New activity available."


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
        self.events: list[dict[str, object]] = []
        self.changed = asyncio.Condition()
        self.staged = asyncio.Event()
        self.release = asyncio.Event()
        self.gate: tuple[str, str] | None = None
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

    def arm(self, surface: Surface, payload: str) -> None:
        self.scope.require_surface(surface, self.surfaces[surface.terminal_id])
        if payload not in {WAKE_TEXT, "/compact", "/clear"} or self.gate is not None:
            raise ProofRefused("invalid stage gate")
        transport = payload + "\n" if payload in {"/compact", "/clear"} else payload
        self.gate = (surface.terminal_id, hashlib.sha256(transport.encode()).hexdigest())
        self.staged.clear()
        self.release.clear()

    async def accept(self, event: dict[str, object]) -> None:
        common = {"terminal_id", "session_id", "project_id", "host_epoch", "phase"}
        wake = event.get("phase") == "wake"
        fields = common | (
            {"delivered", "skipped", "priority", "monotonic"}
            if wake
            else {"origin", "kind", "submit", "payload_sha256", "action_sha256", "outcome"}
        )
        if set(event) - fields or fields - {"outcome"} - set(event):
            raise ProofRefused("invalid trace event fields")
        if event.get("phase") not in {"before", "after", "wake"}:
            raise ProofRefused("invalid trace phase")
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
        self.scope.require_surface(expected, observed)
        if wake:
            if (
                event.get("priority") not in {"normal", "urgent"}
                or type(event.get("delivered")) is not bool
                or not isinstance(event.get("monotonic"), (int, float))
                or event.get("skipped")
                not in {
                    None,
                    "composer_occupied",
                    "composer_unknown",
                    "composer_probe_error",
                    "session_active",
                    "debounced",
                }
            ):
                raise ProofRefused("invalid wake outcome event")
        elif event.get("origin") not in {"operator", "automatic", "attention", "daemon"}:
            raise ProofRefused("unknown write origin")
        if not wake and event["origin"] != "operator":
            allowed = {
                hashlib.sha256(value.encode()).hexdigest()
                for value in (WAKE_TEXT, "/compact", "/compact\n", "/clear", "/clear\n", "enter")
            }
            if event.get("payload_sha256") not in allowed:
                raise ProofRefused("unexpected automatic bytes")
        async with self.changed:
            self.events.append({**event, "sequence": len(self.events) + 1})
            self.changed.notify_all()
        if (
            event.get("phase") == "after"
            and event.get("kind") == "text"
            and event.get("outcome") == "Delivered"
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
