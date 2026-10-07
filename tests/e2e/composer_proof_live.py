"""Concrete real-provider driver. All access is to one isolated fixture daemon."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import httpx
from websockets.protocol import State

from gobby.agents.detection.provider import DetectionRegistry
from gobby.agents.idle_detector import IdleDetector, plain_text
from gobby.storage.terminals import AttachLocator
from gobby.terminals.frame_client import FrameClient
from gobby.terminals.host_protocol import control_socket_path, frames_socket_path
from tests.e2e.composer_proof import (
    ProofRefused,
    ProofScope,
    Surface,
    owned_editor_cleanup,
    safe_evidence,
)
from tests.e2e.composer_proof_cleanup import quiesce_proof
from tests.e2e.composer_proof_frames import ProofFrame, read_frame
from tests.e2e.composer_proof_trace import WAKE_TEXT, ProofTrace
from tests.e2e.conftest import DaemonInstance, MCPTestClient, daemon_token
from tests.e2e.test_terminal_client_stack import WsSession, _attach_locator, _ws_create

if TYPE_CHECKING:
    from tests.e2e.composer_proof_races import ComposerRaces


class ProofWsSession(WsSession):
    def __init__(self, daemon: DaemonInstance) -> None:
        super().__init__(daemon)
        self._proof_subscribed = False

    async def connect(self) -> None:
        self._proof_subscribed = False
        start = len(self.messages)
        await super().connect()
        events = {"hook_event", "session_event"}
        await self.send({"type": "subscribe", "events": sorted(events)})
        try:
            ack = await self.wait_for(
                lambda event: event.get("type") == "subscribe_success"
                and event in self.messages[start:],
                timeout=8,
                description="proof event subscription acknowledgement",
            )
        except (AssertionError, TimeoutError) as exc:
            raise ProofRefused("proof event subscription acknowledgement unavailable") from exc
        acknowledged = ack.get("events")
        if (
            not isinstance(acknowledged, list)
            or any(not isinstance(event, str) for event in acknowledged)
            or not events.issubset(acknowledged)
        ):
            raise ProofRefused("proof event subscription acknowledgement incomplete")
        self._proof_subscribed = True
        self.require_events()

    def require_events(self) -> None:
        if (
            not self._proof_subscribed
            or self._task is None
            or self._task.done()
            or self._ws is None
            or self._ws.state != State.OPEN
        ):
            raise ProofRefused("proof hook/session stream unavailable")

    async def close(self) -> None:
        self._proof_subscribed = False
        await super().close()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)


class Frames:
    def __init__(self, client: FrameClient) -> None:
        self.client = client
        self.latest: ProofFrame | None = None
        self.revision = 0
        self.changed = asyncio.Condition()
        self.failure: Exception | None = None
        self.task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            while True:
                message = await self.client.read_message()
                if message.get("type") in {"error", "terminal_exited"}:
                    raise ProofRefused("native frame surface ended")
                if message.get("type") != "frame":
                    continue
                async with self.changed:
                    self.revision += 1
                    try:
                        self.latest = read_frame(message)
                    except ProofRefused:
                        self.latest = None
                    self.changed.notify_all()
        except (OSError, EOFError, RuntimeError) as exc:
            async with self.changed:
                self.failure = exc
                self.changed.notify_all()

    async def wait_for(
        self, predicate: Callable[[ProofFrame], bool], *, after: int = -1, timeout: float = 30
    ) -> ProofFrame:
        async with asyncio.timeout(timeout), self.changed:
            await self.changed.wait_for(
                lambda: self.failure is not None
                or (self.revision > after and self.latest is not None and predicate(self.latest))
            )
            if self.failure is not None:
                raise ProofRefused("native frame reader failed") from self.failure
            if self.latest is None:
                raise ProofRefused("native frame is unconfirmed")
            return self.latest

    async def close(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.client.close()


@dataclass
class Seat:
    surface: Surface
    external_id: str
    ws: ProofWsSession
    frames: Frames
    detector: IdleDetector
    locator: AttachLocator


class LiveProof:
    def __init__(
        self,
        daemon: DaemonInstance,
        scope: ProofScope,
        trace: ProofTrace,
        detection_registry: DetectionRegistry,
    ) -> None:
        self.daemon, self.scope, self.trace = daemon, scope, trace
        token = daemon_token(daemon.gobby_home)
        self.http = httpx.AsyncClient(
            base_url=daemon.http_url, headers={"Authorization": f"Bearer {token}"}, timeout=10
        )
        self.mcp = MCPTestClient(daemon.http_url, token)
        self.leases: set[tuple[str, str]] = set()
        self.seats: list[Seat] = []
        self.evidence: list[dict[str, object]] = []
        self.created_terminal_ids: list[str] = []
        self.creation_uncertain = False
        self.handoffs_pending: set[str] = set()
        self.terminated: dict[str, Surface] = {}
        self.detection_registry = detection_registry
        self.matrix_passed = False
        self.races_passed = False
        self.races: ComposerRaces

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if (server, tool) not in self.leases:
            await asyncio.to_thread(self.mcp.get_tool_schema, server, tool)
            self.leases.add((server, tool))
        raw = await asyncio.to_thread(self.mcp.call_tool, server, tool, arguments)
        result = raw.get("result", raw)
        if not isinstance(result, dict) or raw.get("success") is False or "error" in result:
            raise ProofRefused(f"isolated {server}:{tool} refused")
        return result

    async def initialize(self) -> None:
        controller = await self.call(
            "gobby-sessions",
            "register_session",
            {
                "external_id": str(uuid4()),
                "source": "codex",
                "project_id": self.scope.project_id,
                "title": "22915-proof-controller",
            },
        )
        self.mcp.session_id = str(controller["session_id"])
        if self.mcp.session_id in self.scope.excluded:
            raise ProofRefused("excluded controller identity")

    async def terminal(self, terminal_id: str) -> dict[str, Any]:
        response = await self.http.get(f"/api/terminals/{terminal_id}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ProofRefused("invalid terminal inventory")
        terminal = result.get("terminal", result)
        if not isinstance(terminal, dict):
            raise ProofRefused("invalid terminal row")
        return terminal

    async def bind(self, provider: str, command: list[str]) -> Seat:
        ws = ProofWsSession(self.daemon)
        client: FrameClient | None = None
        adopted = False
        try:
            await ws.connect()
            self.creation_uncertain = True
            created = await _ws_create(self.daemon, command)
            if created.get("success") is not True or not created.get("terminal_id"):
                raise ProofRefused("proof provider create failed")
            terminal_id = str(created["terminal_id"])
            self.created_terminal_ids.append(terminal_id)
            self.creation_uncertain = False
            # Consume registration events, rereading the authoritative row after each.
            start = 0
            async with asyncio.timeout(90):
                item = await self.terminal(terminal_id)
                while not item.get("session_id"):
                    await ws.wait_for(
                        lambda event, baseline=start: event.get("type") == "session_event"
                        and event.get("project_id") == self.scope.project_id
                        and event in ws.messages[baseline:],
                        timeout=90,
                        description="real provider registration",
                    )
                    start = len(ws.messages)
                    item = await self.terminal(terminal_id)
            locator = self.locator(item)
            own = Surface(
                provider,
                str(item.get("session_id")),
                terminal_id,
                locator.frame_host_epoch,
                str(item.get("project_id")),
            )
            self.scope.require_surface(own, own)
            details = await self.call(
                "gobby-sessions", "get_session", {"session_id": own.session_id}
            )
            if (
                details.get("source") != provider
                or details.get("project_id") != self.scope.project_id
            ):
                raise ProofRefused("real provider registration mismatch")
            await ws.attach(terminal_id, delivery="direct", request_id=f"proof-{provider}")
            control = await ws.take(terminal_id, takeover=False)
            if control.get("granted") is not True:
                raise ProofRefused("proof control refused")
            if locator.host_socket is None:
                raise ProofRefused("native host socket missing")
            socket_dir = Path(locator.host_socket).parent
            reader, writer = await asyncio.open_unix_connection(frames_socket_path(socket_dir))
            client = FrameClient(reader, writer)
            await client.handshake(locator, local_token=daemon_token(socket_dir))
            await client.attach_terminal(locator)
            seat = Seat(
                own,
                str(details["external_id"]),
                ws,
                Frames(client),
                IdleDetector(self.detection_registry, provider),
                locator,
            )
            self.trace.register(own)
            self.seats.append(seat)
            adopted = True
            await self.empty(seat, timeout=120)
            return seat
        finally:
            if not adopted:
                if client is not None:
                    await client.close()
                await ws.close()

    def locator(self, row: dict[str, Any]) -> AttachLocator:
        locator = _attach_locator(row)
        if (
            locator.backend != "native"
            or locator.host_socket != str(control_socket_path(self.scope.root / "h"))
            or not locator.host_terminal_id
        ):
            raise ProofRefused("native host binding outside private proof root")
        return locator

    async def validate(self, seat: Seat) -> dict[str, Any]:
        row = await self.terminal(seat.surface.terminal_id)
        locator = _attach_locator(row)
        observed = Surface(
            seat.surface.provider,
            str(row.get("session_id")),
            str(row.get("id")),
            locator.frame_host_epoch,
            str(row.get("project_id")),
        )
        self.scope.require_surface(seat.surface, observed)
        if self.locator(row) != seat.locator:
            raise ProofRefused("native attachment binding changed")
        return row

    async def empty(self, seat: Seat, *, timeout: float = 30, after: int = -1) -> ProofFrame:
        await seat.frames.wait_for(
            lambda frame: (
                seat.detector.composer_read(frame.ansi).state == "empty"
                and seat.detector.turn_in_flight_fingerprint(frame.ansi) is None
            ),
            after=after,
            timeout=timeout,
        )
        await self.validate(seat)
        start = len(seat.ws.messages)
        async with asyncio.timeout(timeout):
            details = await self.call(
                "gobby-sessions", "get_session", {"session_id": seat.surface.session_id}
            )
            while details.get("status") != "paused":
                await seat.ws.wait_for(
                    lambda event, baseline=start: event.get("type") == "session_event"
                    and event.get("session_id") == seat.surface.session_id
                    and event in seat.ws.messages[baseline:],
                    timeout=timeout,
                    description="real paused lifecycle",
                )
                start = len(seat.ws.messages)
                details = await self.call(
                    "gobby-sessions", "get_session", {"session_id": seat.surface.session_id}
                )
        return await seat.frames.wait_for(
            lambda current: seat.detector.composer_read(current.ansi).state == "empty"
            and seat.detector.turn_in_flight_fingerprint(current.ansi) is None,
            after=after,
            timeout=timeout,
        )

    async def edit(
        self, seat: Seat, data: str, *, expected: tuple[str, tuple[int, int]] | None = None
    ) -> None:
        await self.validate(seat)
        if expected is not None:
            latest = seat.frames.latest
            if (
                latest is None
                or (
                    seat.detector.composer_read(latest.ansi).line,
                    latest.cursor,
                )
                != expected
            ):
                raise ProofRefused("owned draft or cursor changed before cleanup")
        outcome = await seat.ws.write(seat.surface.terminal_id, data)
        if outcome.get("status") != "delivered" and outcome.get("outcome") != "delivered":
            raise ProofRefused("owned editor input unconfirmed")

    async def held_boundary(
        self,
        seat: Seat,
        command: str,
        *,
        after: int,
        hooks_after: int,
        before_write: bool = False,
    ) -> ProofFrame:
        """Observe an armed actual write; this never starts or claims a race."""
        if command not in {"/compact", "/clear", WAKE_TEXT}:
            raise ProofRefused("unsupported held boundary command")
        transport = command + "\n" if command in {"/compact", "/clear"} else command
        gate = (seat.surface.terminal_id, hashlib.sha256(transport.encode()).hexdigest())
        phase = "before" if before_write else "after"

        def held(value: ProofFrame) -> bool:
            read = seat.detector.composer_read(value.ansi)
            return (
                read.state == "empty"
                if before_write
                else (read.state == "draft" and read.line == command)
            )

        try:
            seat.ws.require_events()
            if (
                self.trace.gate != gate
                or self.trace.release.is_set()
                or self.trace.gate_phase != phase
            ):
                raise ProofRefused("held boundary is not armed")
            await asyncio.wait_for(self.trace.staged.wait(), 10)
            if before_write:
                await seat.frames.client.set_scroll_offset(0)
            frame = await seat.frames.wait_for(
                held,
                after=after,
                timeout=10,
            )
            await self.validate(seat)
            seat.ws.require_events()
            current = seat.frames.latest
            if (
                self.trace.gate != gate
                or self.trace.release.is_set()
                or current is None
                or current.cursor != frame.cursor
                or not held(current)
                or any(
                    event.get("type") == "hook_event"
                    and event.get("session_id") == seat.external_id
                    and event.get("event_type")
                    in {"user-prompt-submit", "session-start", "post-compact"}
                    for event in seat.ws.messages[hooks_after:]
                )
            ):
                raise ProofRefused("held boundary already submitted or released")
            frame = current
        except (TimeoutError, ProofRefused) as exc:
            self.trace.release.set()
            raise ProofRefused("real held boundary unavailable; refuse timing-only race") from exc
        self.evidence.append(
            {
                "held_command": command,
                "before_write": before_write,
                **safe_evidence(seat.surface, frame.ansi, frame.cursor),
            }
        )
        return frame

    async def notice(self, seat: Seat, priority: str) -> str:
        await self.validate(seat)
        marker = f"R2_22915_{seat.surface.provider.upper()}_{priority.upper()}_NOTICE"
        before = seat.frames.revision
        result = await self.call(
            "gobby-agents",
            "send_message",
            {
                "target": "session",
                "target_id": seat.surface.session_id,
                "content": marker,
                "priority": priority,
                "wake": True,
            },
        )
        ids = result.get("message_ids")
        if not isinstance(ids, list) or len(ids) != 1:
            raise ProofRefused("proof notice has no unique durable ID")
        # Force this read attachment's view even when the native host deduplicates unchanged frames.
        await seat.frames.client.set_scroll_offset(0)
        frame = await seat.frames.wait_for(lambda frame: True, after=before)
        self.evidence.append(
            {
                "case": priority,
                "message_id": ids[0],
                **safe_evidence(
                    seat.surface,
                    frame.ansi,
                    frame.cursor,
                ),
            }
        )
        return str(ids[0])

    async def durable(self, target: Surface, message_id: str) -> None:
        self.scope.require_surface(target, target)
        result = await self.call(
            "gobby-agents", "get_inter_session_message", {"message_id": message_id}
        )
        message = result.get("message", result)
        if message.get("id") != message_id or message.get("to_session") != target.session_id:
            raise ProofRefused("durable proof notice missing or rebound")
        self.evidence.append(
            {"message_id": message_id, "durable": True, "delivered_at": message.get("delivered_at")}
        )

    async def submitted(
        self, seat: Seat, *, writes_after: int, hooks_after: int, timeout: float
    ) -> None:
        enter_hash = hashlib.sha256(b"enter").hexdigest()
        revision = seat.frames.revision
        await self.trace.wait_for(
            lambda event: event.get("phase") == "after"
            and event.get("terminal_id") == seat.surface.terminal_id
            and event.get("payload_sha256") == enter_hash
            and event.get("outcome") == "Delivered",
            after=writes_after,
            timeout=timeout,
        )
        event = await seat.ws.wait_for(
            lambda event: event.get("type") == "hook_event"
            and event.get("event_type") == "user-prompt-submit"
            and event.get("session_id") == seat.external_id
            and event.get("data", {}).get("prompt_text", event.get("data", {}).get("prompt"))
            == WAKE_TEXT
            and event in seat.ws.messages[hooks_after:],
            timeout=30,
            description="real wake submission hook",
        )
        await self.empty(seat, timeout=120, after=revision)
        writes = [
            item
            for item in self.trace.events[writes_after:]
            if item.get("phase") == "after"
            and item.get("terminal_id") == seat.surface.terminal_id
            and item.get("origin") != "operator"
        ]
        if (
            len(writes) != 2
            or writes[0].get("payload_sha256") != hashlib.sha256(WAKE_TEXT.encode()).hexdigest()
        ):
            raise ProofRefused("duplicate or unexpected automatic submission")
        self.evidence.append(
            {
                "session_id": seat.surface.session_id,
                "submitted_prompt_sha256": hashlib.sha256(WAKE_TEXT.encode()).hexdigest(),
                "hook_timestamp": event.get("timestamp"),
                "writes": writes,
            }
        )

    async def empty_wake(self, seat: Seat, priority: str) -> None:
        await self.empty(seat)
        start, hooks = len(self.trace.events), len(seat.ws.messages)
        message = await self.notice(seat, priority)
        await self.submitted(seat, writes_after=start, hooks_after=hooks, timeout=30)
        await self.durable(seat.surface, message)

    async def occupied_wake(self, seat: Seat, priority: str) -> None:
        await self.empty(seat)
        draft = f"R2_22915_{seat.surface.provider.upper()}_DRAFT_ABCD_EFGH"
        revision = seat.frames.revision
        await self.edit(seat, draft + "\x1b[D" * 5)

        def owns_draft(frame: ProofFrame, expected: str = draft) -> bool:
            return seat.detector.composer_read(frame.ansi).line == expected

        before = await seat.frames.wait_for(owns_draft, after=revision)
        row = plain_text(before.ansi).splitlines()[before.cursor[1]]
        offset = row.find(draft)
        if offset < 0 or before.cursor[0] != offset + len(draft) - 5:
            raise ProofRefused("owned draft cursor unconfirmed")
        start, hooks = len(self.trace.events), len(seat.ws.messages)
        message = await self.notice(seat, priority)
        # Genuine dispatcher attempts, including the retry ladder. No snapshot polling.
        async with asyncio.timeout(130):
            attempts: list[dict[str, object]] = []
            for count in range(4):
                attempt = await self.trace.wait_for(
                    lambda event: event.get("phase") == "wake"
                    and event.get("session_id") == seat.surface.session_id,
                    after=start if not attempts else cast(int, attempts[-1]["sequence"]),
                    timeout=70 if count else 30,
                )
                attempts.append(attempt)
                if (
                    attempt.get("delivered") is not False
                    or attempt.get("skipped") != "composer_occupied"
                ):
                    raise ProofRefused("occupied wake was not withheld")
            gaps = [
                cast(float, attempts[i]["monotonic"]) - cast(float, attempts[i - 1]["monotonic"])
                for i in range(1, 4)
            ]
            if any(
                abs(actual - expected) > 5
                for actual, expected in zip(gaps, (15, 30, 60), strict=True)
            ):
                raise ProofRefused("actual retry ladder mismatch")
        latest = await seat.frames.wait_for(lambda frame: True)
        if seat.detector.composer_read(latest.ansi).line != draft or latest.cursor != before.cursor:
            raise ProofRefused("owned draft or cursor changed")
        if any(
            item.get("phase") in {"before", "after"} and item.get("origin") != "operator"
            for item in self.trace.events[start:]
        ):
            raise ProofRefused("automatic bytes reached occupied composer")
        await self.durable(seat.surface, message)
        self.evidence.append(
            {
                "case": f"occupied-{priority}",
                "attempts": attempts,
                **safe_evidence(seat.surface, latest.ansi, latest.cursor),
            }
        )
        revision = seat.frames.revision
        await self.edit(
            seat,
            owned_editor_cleanup(seat.surface.provider, draft, 5),
            expected=(draft, before.cursor),
        )
        await self.empty(seat, after=revision)
        async with asyncio.timeout(250):
            await self.submitted(seat, writes_after=start, hooks_after=hooks, timeout=250)
        await self.durable(seat.surface, message)

    async def matrix(self, seat: Seat) -> None:
        await self.empty_wake(seat, "normal")
        await self.occupied_wake(seat, "normal")
        await self.occupied_wake(seat, "urgent")
        await self.empty_wake(seat, "urgent")
        runtime = json.loads((self.trace.socket.parent / "runtime.json").read_text())
        checkout = Path(__file__).resolve().parents[2]
        if (
            runtime.get("gobby_module") != str(checkout / "src/gobby/__init__.py")
            or runtime.get("fixture_module") != str(checkout / "tests/e2e/composer_proof_runner.py")
            or runtime.get("python_executable") != sys.executable
            or runtime.get("python_version") != sys.version.split()[0]
        ):
            raise ProofRefused("actual runner source/interpreter mismatch")
        manifest = self.detection_registry.for_provider(seat.surface.provider)
        if (
            manifest is None
            or runtime.get("detection", {}).get(seat.surface.provider) != manifest.fingerprint
        ):
            raise ProofRefused("active daemon detection identity mismatch")
        self.evidence.append({"runtime": runtime})

    async def terminate(self, seat: Seat) -> None:
        self.scope.require_surface(seat.surface, seat.surface)
        if seat not in self.seats:
            raise ProofRefused("unowned proof termination")
        await self.validate(seat)
        recorded = self.terminated.get(seat.surface.terminal_id)
        if recorded is not None and recorded != seat.surface:
            raise ProofRefused("terminated proof binding changed")
        if recorded is None:
            request = f"kill-{uuid4()}"
            await seat.ws.send(
                {
                    "type": "terminal_kill",
                    "terminal_id": seat.surface.terminal_id,
                    "request_id": request,
                }
            )
            outcome = await seat.ws.wait_for(
                lambda event: event.get("type") == "terminal_kill_result"
                and event.get("request_id") == request,
                timeout=10,
                description="owned proof termination",
            )
            if outcome.get("success") is not True:
                raise ProofRefused("owned proof termination unconfirmed")
        row = await self.validate(seat)
        if row.get("state") != "exited":
            raise ProofRefused("owned proof termination has no exited readback")
        if recorded is None:
            self.terminated[seat.surface.terminal_id] = seat.surface
            self.evidence.append({"case": "owned_terminal_terminated", **seat.surface.__dict__})

    async def close(self) -> None:
        self.trace.release.set()
        self.trace.admission_release.set()
        self.trace.acquisition_release.set()
        failures: list[Exception] = []
        quiesced = False
        try:
            await quiesce_proof(
                self.trace.socket.parent / "cleanup.sock", [seat.surface for seat in self.seats]
            )
            if self.handoffs_pending:
                raise ProofRefused("unsettled proof handoff; preserve owned terminal")
            quiesced = True
        except (ProofRefused, TimeoutError, OSError, ValueError) as exc:
            failures.append(exc)
        for seat in self.seats:
            try:
                if not quiesced:
                    raise ProofRefused("retry cleanup unconfirmed; preserve owned terminal")
                await self.terminate(seat)
            except (
                RuntimeError,
                TimeoutError,
                httpx.HTTPError,
                AssertionError,
                ValueError,
                OSError,
            ) as exc:
                failures.append(exc)
            finally:
                results = await asyncio.gather(
                    seat.frames.close(), seat.ws.close(), return_exceptions=True
                )
                failures.extend(result for result in results if isinstance(result, Exception))
        self.mcp.close()
        await self.http.aclose()
        if self.creation_uncertain or set(self.created_terminal_ids) != {
            seat.surface.terminal_id for seat in self.seats
        }:
            failures.append(ProofRefused("unbound proof creation requires owner cleanup"))
        if failures:
            raise ProofRefused(
                "proof cleanup refused; preserve isolated state for owner"
            ) from failures[0]
