"""Exercise the proof inbox through the real subscription and broadcast protocol."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from websockets.protocol import State

from gobby.servers.websocket.broadcast import BroadcastMixin
from gobby.servers.websocket.handlers.core import HandlerMixin
from gobby.storage.config_mutations import ConfigPatch
from tests.e2e import test_composer_live_proof as fixture_module
from tests.e2e.composer_proof import ProofRefused, ProofScope, Surface
from tests.e2e.composer_proof_admission import Admission
from tests.e2e.composer_proof_frames import ProofFrame
from tests.e2e.composer_proof_live import LiveProof, ProofWsSession, Seat
from tests.e2e.composer_proof_trace import ProofTrace
from tests.e2e.conftest import DaemonInstance
from tests.terminals.test_composer_proof_admission import admission_data

pytestmark = pytest.mark.unit


class SubscriptionTransport:
    """Replace only socket I/O; the daemon handler/filter and client pump stay real."""

    def __init__(self, ack_events: list[object] | None = None) -> None:
        self.state = State.OPEN
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.queue.put_nowait('{"type":"welcome"}')
        self.requests: list[dict[str, Any]] = []
        self.ack_events = ack_events
        self.server = SimpleNamespace(
            subscriptions=set[str](), user_id="proof", send=self.server_send
        )

    async def recv(self) -> str:
        result = await self.queue.get()
        assert isinstance(result, str)
        return result

    async def send(self, raw: str) -> None:
        request = json.loads(raw)
        self.requests.append(request)
        assert request["type"] == "subscribe"
        await HandlerMixin()._handle_subscribe(self.server, request)

    async def server_send(self, raw: str) -> None:
        message = json.loads(raw)
        if message["type"] == "subscribe_success" and self.ack_events is not None:
            message["events"] = self.ack_events
        await self.queue.put(json.dumps(message))

    async def broadcast(self, event: dict[str, Any]) -> None:
        if BroadcastMixin()._is_subscribed(self.server, event):
            await self.server_send(json.dumps(event))

    async def __aiter__(self) -> AsyncIterator[str]:
        while (raw := await self.queue.get()) is not None:
            yield raw

    async def close(self) -> None:
        self.state = State.CLOSED
        await self.queue.put(None)


def proof_ws(tmp_path: Path) -> ProofWsSession:
    daemon = cast(DaemonInstance, SimpleNamespace(gobby_home=tmp_path, ws_url="ws://proof.invalid"))
    return ProofWsSession(daemon)


@pytest.mark.parametrize("event_type", ["user-prompt-submit", "session-start", "post-compact"])
async def test_proof_connect_subscribes_and_receives_real_filtered_events(
    tmp_path: Path, event_type: str
) -> None:
    transport = SubscriptionTransport()
    ws = proof_ws(tmp_path)
    # Unsubscribed clients really lose these messages at the production filter.
    assert not BroadcastMixin()._is_subscribed(transport.server, {"type": "hook_event"})
    with (
        patch("tests.e2e.test_terminal_client_stack.daemon_token", return_value="owned-test-token"),
        patch(
            "tests.e2e.test_terminal_client_stack.websockets.connect",
            AsyncMock(return_value=transport),
        ),
    ):
        try:
            await ws.connect()
            assert transport.server.subscriptions == {"hook_event", "session_event"}
            hook = {"type": "hook_event", "event_type": event_type, "session_id": "external"}
            session = {"type": "session_event", "session_id": "canonical", "event": "updated"}
            for event in (hook, session):
                await transport.broadcast(event)
                assert (
                    await ws.wait_for(
                        lambda item, expected=event: item == expected,
                        timeout=0.1,
                        description="filtered proof event",
                    )
                    == event
                )
        finally:
            await ws.close()


@pytest.mark.parametrize(
    "ack_events", [[], ["hook_event"], ["session_event"], ["hook_event", ["session_event"]]]
)
async def test_proof_connect_refuses_incomplete_subscription_ack(
    tmp_path: Path, ack_events: list[object]
) -> None:
    transport = SubscriptionTransport(ack_events)
    ws = proof_ws(tmp_path)
    with (
        patch("tests.e2e.test_terminal_client_stack.daemon_token", return_value="owned-test-token"),
        patch(
            "tests.e2e.test_terminal_client_stack.websockets.connect",
            AsyncMock(return_value=transport),
        ),
    ):
        try:
            with pytest.raises(ProofRefused, match="subscription"):
                await ws.connect()
        finally:
            await ws.close()


def held_seat(
    tmp_path: Path, ws: ProofWsSession, provider: str, command: str
) -> tuple[LiveProof, Seat]:
    own = Surface(provider, str(uuid4()), str(uuid4()), "proof-epoch", str(uuid4()))
    frame = ProofFrame(command, (len(command), 0))
    proof = LiveProof.__new__(LiveProof)
    proof.evidence = []
    proof.trace = ProofTrace(
        ProofScope(tmp_path, own.project_id, frozenset({str(uuid4())}), "a" * 40), tmp_path
    )
    proof.trace.register(own)
    proof.trace.arm(own, command)
    proof.trace.staged.set()
    seat = cast(
        Seat,
        SimpleNamespace(
            surface=own,
            external_id="proof-external",
            ws=ws,
            frames=SimpleNamespace(wait_for=AsyncMock(return_value=frame), latest=frame),
            detector=SimpleNamespace(
                composer_read=lambda ansi: SimpleNamespace(state="draft", line=ansi)
            ),
        ),
    )
    return proof, seat


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("command", ["/compact", "/clear"])
@pytest.mark.parametrize("event_type", ["user-prompt-submit", "post-compact"])
async def test_held_boundary_rejects_hook_received_through_subscription(
    tmp_path: Path, provider: str, command: str, event_type: str
) -> None:
    transport = SubscriptionTransport()
    ws = proof_ws(tmp_path)
    with (
        patch("tests.e2e.test_terminal_client_stack.daemon_token", return_value="owned-test-token"),
        patch(
            "tests.e2e.test_terminal_client_stack.websockets.connect",
            AsyncMock(return_value=transport),
        ),
    ):
        try:
            await ws.connect()
            baseline = len(ws.messages)
            event = {"type": "hook_event", "event_type": event_type, "session_id": "proof-external"}
            await transport.broadcast(event)
            await ws.wait_for(
                lambda item: item == event, timeout=0.1, description="submitted hook receipt"
            )
            proof, seat = held_seat(tmp_path, ws, provider, command)
            with patch.object(proof, "validate", AsyncMock()):
                with pytest.raises(ProofRefused, match="held boundary"):
                    await proof.held_boundary(seat, command, after=0, hooks_after=baseline)
            assert event in ws.messages[baseline:], "the real inbox must receive the submitted hook"
            assert proof.trace.release.is_set()
            assert proof.evidence == []
        finally:
            await ws.close()


@pytest.mark.parametrize("stream", ["never-connected", "disconnected"])
async def test_held_boundary_refuses_an_unavailable_hook_stream(
    tmp_path: Path, stream: str
) -> None:
    transport = SubscriptionTransport()
    ws = proof_ws(tmp_path)
    with (
        patch("tests.e2e.test_terminal_client_stack.daemon_token", return_value="owned-test-token"),
        patch(
            "tests.e2e.test_terminal_client_stack.websockets.connect",
            AsyncMock(return_value=transport),
        ),
    ):
        try:
            if stream == "disconnected":
                await ws.connect()
                await transport.close()
                assert ws._task is not None
                await ws._task
            proof, seat = held_seat(tmp_path, ws, "codex", "/compact")
            with patch.object(proof, "validate", AsyncMock()):
                with pytest.raises(ProofRefused, match="held boundary"):
                    await proof.held_boundary(seat, "/compact", after=0, hooks_after=0)
            assert proof.trace.release.is_set()
            assert proof.evidence == []
        finally:
            await ws.close()


async def test_isolated_fixture_enables_post_compact_before_any_launch(tmp_path: Path) -> None:
    spec = Admission.model_validate(admission_data(datetime.now(UTC)))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = tmp_path / "socket-root"
    root.mkdir(mode=0o700)
    captured: list[ConfigPatch] = []

    def capture_config(*, patch: ConfigPatch, **kwargs: Any) -> None:
        captured.append(patch)
        events = patch.values["hook_extensions.websocket.broadcast_events"]
        assert isinstance(events, list) and "post-compact" in events
        raise ProofRefused("configuration captured before launch")

    mutations = SimpleNamespace(
        repository=SimpleNamespace(current_revision=Mock(return_value=0)),
        patch_internal=capture_config,
    )
    with (
        patch.object(fixture_module, "verify_binary_set", return_value=tmp_path / "native"),
        patch.object(fixture_module, "link_existing_auth") as auth,
        patch.object(fixture_module, "link_operator_srt") as srt,
        patch("tests.e2e.test_composer_live_proof.tempfile.mkdtemp", return_value=str(root)),
        patch.object(fixture_module, "ConfigMutations", return_value=mutations),
        patch("tests.e2e.test_composer_live_proof.subprocess.Popen") as launch,
    ):
        fixture = inspect.unwrap(fixture_module.composer_fixture)
        with pytest.raises(ProofRefused, match="configuration captured"):
            await anext(fixture(spec, tmp_path, (home / "config.yaml", 1, 2), None))
        assert len(captured) == 1
        assert auth.call_count == 2, (
            "auth linking is replaced with a stub; no real auth is accessed"
        )
        launch.assert_not_called()
        srt.assert_called_once_with(home)
