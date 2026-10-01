"""Driver refusal checks never connect to a daemon or launch a provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from gobby.storage.sessions._constants import ALLOWED_SESSION_STATUSES
from gobby.terminals.frame_client import FrameClient
from tests.e2e.composer_proof import ProofRefused, ProofScope, Surface, require_private_root
from tests.e2e.composer_proof_frames import ProofFrame
from tests.e2e.composer_proof_live import Frames, LiveProof, Seat
from tests.e2e.composer_proof_setup import verify_setup
from tests.e2e.composer_proof_trace import ProofTrace

pytestmark = pytest.mark.unit


def test_sealed_home_remains_a_valid_private_fixture_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".gobby-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    assert require_private_root(home) == home.resolve()


@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_empty_wait_uses_real_update_event_then_rereads_paused_status(provider: str) -> None:
    own = Surface(provider, str(uuid4()), str(uuid4()), "proof-epoch", str(uuid4()))
    messages: list[dict[str, Any]] = []

    async def update(predicate: Callable[[dict[str, Any]], bool], **kwargs: Any) -> dict[str, Any]:
        event = {"type": "session_event", "session_id": own.session_id, "event": "updated"}
        messages.append(event)
        assert predicate(event), "real update events have no status field"
        return event

    wait = AsyncMock(side_effect=update)
    frame = ProofFrame("empty", (0, 0))
    seat = cast(
        Seat,
        SimpleNamespace(
            surface=own,
            ws=SimpleNamespace(messages=messages, wait_for=wait),
            frames=SimpleNamespace(wait_for=AsyncMock(return_value=frame)),
            detector=SimpleNamespace(
                composer_read=lambda _: SimpleNamespace(state="empty"),
                turn_in_flight_fingerprint=lambda _: None,
            ),
        ),
    )
    proof = LiveProof.__new__(LiveProof)
    statuses = ["active", "paused"]
    assert set(statuses) <= ALLOWED_SESSION_STATUSES
    read = AsyncMock(side_effect=[{"status": status} for status in statuses])
    with patch.object(proof, "call", read), patch.object(proof, "validate", AsyncMock()):
        assert await proof.empty(seat) == frame
    assert read.await_count == 2
    wait.assert_awaited_once()


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("priority", ["normal", "urgent"])
async def test_notice_waits_for_a_new_atomic_frame(provider: str, priority: str) -> None:
    own = Surface(provider, str(uuid4()), str(uuid4()), "proof-epoch", str(uuid4()))
    frames = Frames.__new__(Frames)
    frames.client = cast(FrameClient, SimpleNamespace(set_scroll_offset=AsyncMock()))
    frames.latest = ProofFrame("frame before notice", (3, 0))
    frames.revision = 7
    frames.changed = asyncio.Condition()
    frames.failure = None
    seat = cast(Seat, SimpleNamespace(surface=own, frames=frames))
    proof = LiveProof.__new__(LiveProof)
    proof.evidence = []
    sent = asyncio.Event()
    message_id = str(uuid4())

    async def send(*args: Any) -> dict[str, Any]:
        sent.set()
        return {"message_ids": [message_id]}

    with (
        patch.object(proof, "call", AsyncMock(side_effect=send)),
        patch.object(proof, "validate", AsyncMock()),
    ):
        task = asyncio.create_task(proof.notice(seat, priority))
        try:
            await asyncio.wait_for(sent.wait(), 1)
            assert not task.done(), "notice evidence must wait beyond the pre-notice frame"
            fresh = ProofFrame("frame after notice", (9, 2))
            async with frames.changed:
                frames.latest = fresh
                frames.revision += 1
                frames.changed.notify_all()
            assert await asyncio.wait_for(task, 1) == message_id
            assert (
                proof.evidence[0]["frame_sha256"] == hashlib.sha256(fresh.ansi.encode()).hexdigest()
            )
            assert proof.evidence[0]["cursor"] == [9, 2]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_notice_refreshes_a_semantically_unchanged_native_frame(provider: str) -> None:
    own = Surface(provider, str(uuid4()), str(uuid4()), "proof-epoch", str(uuid4()))
    frame = ProofFrame("unchanged owned draft", (7, 1))
    frames = Frames.__new__(Frames)
    frames.client = FrameClient.__new__(FrameClient)
    frames.latest = frame
    frames.revision = 12
    frames.changed = asyncio.Condition()
    frames.failure = None
    seat = cast(Seat, SimpleNamespace(surface=own, frames=frames))
    proof = LiveProof.__new__(LiveProof)
    proof.evidence = []
    message_id = str(uuid4())
    sent = asyncio.Event()

    async def send_notice(*args: Any) -> dict[str, Any]:
        sent.set()
        return {"message_ids": [message_id]}

    async def host_view_reply(payload: dict[str, Any]) -> None:
        assert sent.is_set(), "the fresh view must be requested after sending the notice"
        assert payload == {"type": "set_scroll_offset", "rows_from_live_edge": 0}
        # The native host forces a new frame for the attachment even when cells/cursor match.
        async with frames.changed:
            frames.revision += 1
            frames.changed.notify_all()

    with (
        patch.object(proof, "call", AsyncMock(side_effect=send_notice)),
        patch.object(proof, "validate", AsyncMock()),
        patch.object(frames.client, "_send", AsyncMock(side_effect=host_view_reply)),
    ):
        assert await asyncio.wait_for(proof.notice(seat, "normal"), 0.1) == message_id
    assert frames.revision == 13
    assert frames.latest == frame
    assert proof.evidence[0]["cursor"] == [7, 1]


def test_setup_requires_the_isolated_project_and_its_private_home(tmp_path: Path) -> None:
    project = tmp_path / "proof"
    project.mkdir()
    marker = project / ".gobby" / "project.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"id": "00000000-0000-0000-0000-000000000e2e"}))
    home = project / ".gobby-home"
    home.mkdir(mode=0o700)
    verify_setup(home, project)
    marker.write_text(json.dumps({"id": "d45545c5-ded5-4335-b115-0245752edacf"}))
    with pytest.raises(ProofRefused, match="isolated"):
        verify_setup(home, project)
    marker.write_text(json.dumps({"id": "00000000-0000-0000-0000-000000000e2e"}))
    with pytest.raises(ProofRefused, match="home"):
        verify_setup(tmp_path, project)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["id", "session_id", "project_id", "epoch", "host_socket"])
async def test_rebound_surface_receives_zero_editor_input(tmp_path: Path, field: str) -> None:
    own = Surface("codex", str(uuid4()), str(uuid4()), "proof-epoch", str(uuid4()))
    row: dict[str, object] = {
        "id": own.terminal_id,
        "session_id": own.session_id,
        "project_id": own.project_id,
        "backend": "native",
        "attach": {
            "backend": "native",
            "frame_host_epoch": own.host_epoch,
            "host_socket": "/proof/control.sock",
            "host_terminal_id": own.terminal_id,
        },
    }
    if field == "epoch":
        row["attach"] = {"backend": "native", "frame_host_epoch": "rebound-epoch"}
    elif field == "host_socket":
        row["attach"] = {
            "backend": "native",
            "frame_host_epoch": own.host_epoch,
            "host_socket": "/foreign/control.sock",
            "host_terminal_id": own.terminal_id,
        }
    else:
        row[field] = str(uuid4())

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=row)

    proof = LiveProof.__new__(LiveProof)
    proof.scope = ProofScope(tmp_path, own.project_id, frozenset({str(uuid4())}), "a" * 40)
    proof.http = httpx.AsyncClient(
        base_url="http://proof.invalid", transport=httpx.MockTransport(respond)
    )
    write = AsyncMock()
    seat = cast(Seat, SimpleNamespace(surface=own, ws=SimpleNamespace(write=write)))
    try:
        with pytest.raises(ProofRefused, match="binding"):
            await proof.edit(seat, "R2_22915_CODEX_DRAFT_ABCD_EFGH")
        write.assert_not_awaited()
    finally:
        await proof.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("command", ["/compact", "/clear"])
@pytest.mark.parametrize("state", ["held", "left", "submitted", "changed"])
async def test_compact_boundary_requires_fresh_held_frame_before_any_race(
    tmp_path: Path, provider: str, command: str, state: str
) -> None:
    own = Surface(provider, str(uuid4()), str(uuid4()), "proof-epoch", str(uuid4()))
    frame = ProofFrame("empty" if state == "left" else command, (len(command), 0))

    async def frame_wait(
        predicate: Callable[[ProofFrame], bool], *, after: int, timeout: float
    ) -> ProofFrame:
        assert after == 12, "pre-staging frames must never authorize the race"
        if not predicate(frame):
            raise TimeoutError
        return frame

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
            frames=SimpleNamespace(wait_for=frame_wait, latest=frame),
            external_id="proof-external",
            ws=SimpleNamespace(
                require_events=lambda: None,
                messages=[
                    {
                        "type": "hook_event",
                        "session_id": "proof-external",
                        "event_type": "session-start",
                    }
                ]
                if state == "submitted"
                else [],
            ),
            detector=SimpleNamespace(
                composer_read=lambda ansi: SimpleNamespace(
                    state="draft" if ansi == command else "empty", line=ansi
                )
            ),
        ),
    )

    async def validate(_: Seat) -> None:
        if state == "changed":
            seat.frames.latest = ProofFrame("empty", (0, 0))

    with patch.object(proof, "validate", AsyncMock(side_effect=validate)):
        if state == "held":
            assert await proof.held_boundary(seat, command, after=12, hooks_after=0) == frame
            assert proof.evidence[0]["held_command"] == command
            assert not proof.trace.release.is_set()
        else:
            with pytest.raises(ProofRefused, match="held boundary"):
                await proof.held_boundary(seat, command, after=12, hooks_after=0)
            assert proof.evidence == []
            assert proof.trace.release.is_set()


@pytest.mark.parametrize("changed", ["text", "cursor"])
async def test_cleanup_rechecks_owned_draft_after_binding_validation(changed: str) -> None:
    draft = "R2_22915_CODEX_DRAFT_ABCD_EFGH"
    frame = ProofFrame(draft if changed == "cursor" else "changed", (0, 0))
    write = AsyncMock()
    seat = cast(
        Seat,
        SimpleNamespace(
            surface=Surface("codex", str(uuid4()), str(uuid4()), "epoch", str(uuid4())),
            frames=SimpleNamespace(latest=frame),
            detector=SimpleNamespace(composer_read=lambda ansi: SimpleNamespace(line=ansi)),
            ws=SimpleNamespace(write=write),
        ),
    )
    proof = LiveProof.__new__(LiveProof)
    with patch.object(proof, "validate", AsyncMock()):
        with pytest.raises(ProofRefused, match="before cleanup"):
            await proof.edit(seat, "\x1b[C" * 5 + "\x7f" * 30, expected=(draft, (25, 0)))
    write.assert_not_awaited()
