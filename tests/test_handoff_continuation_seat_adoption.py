"""The handoff runner attaches its seat without waiting out the compact window."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.e2e import handoff_continuation_runner as runner
from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_live import LiveProof

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("source", ["claude", "wrong-provider"])
async def test_handoff_adoption_validates_registration_and_never_waits_for_idle(
    source: str,
) -> None:
    proof = MagicMock(spec=LiveProof)
    proof.daemon = MagicMock()
    proof.scope = MagicMock(project_id="isolated-project")
    proof.created_terminal_ids = []
    proof.seats = []
    proof.trace = MagicMock()
    proof.detection_registry = MagicMock()
    proof.terminal = AsyncMock(
        return_value={
            "session_id": "seat-1",
            "project_id": "isolated-project",
        }
    )
    proof.call = AsyncMock(
        return_value={
            "source": source,
            "project_id": "isolated-project",
            "external_id": "provider-seat",
        }
    )
    proof.locator = MagicMock(
        return_value=MagicMock(
            frame_host_epoch="epoch-1",
            host_socket=str(Path("/fixture/host/control.sock")),
        )
    )
    proof.empty = AsyncMock()
    ws = MagicMock()
    ws.connect = AsyncMock()
    ws.attach = AsyncMock()
    ws.take = AsyncMock(return_value={"granted": True})
    ws.close = AsyncMock()
    client = MagicMock()
    client.handshake = AsyncMock()
    client.attach_terminal = AsyncMock()
    command = ["/fixture/provider", "stage a handoff"]
    created = AsyncMock(return_value={"success": True, "terminal_id": "terminal-1"})
    with (
        patch.object(runner, "ProofWsSession", return_value=ws, create=True),
        patch.object(runner, "_ws_create", created, create=True),
        patch.object(runner, "FrameClient", return_value=client, create=True),
        patch.object(runner, "Frames", create=True),
        patch.object(runner, "IdleDetector", create=True),
        patch.object(runner, "daemon_token", return_value="fixture-token", create=True),
        patch.object(runner, "asyncio", create=True) as aio,
    ):
        # Keep the real timeout context and replace only the socket boundary.
        import asyncio

        aio.timeout = asyncio.timeout
        aio.open_unix_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))
        if source == "wrong-provider":
            with pytest.raises(ProofRefused, match="registration mismatch"):
                await runner.adopt(proof, "claude", command)
            ws.close.assert_awaited_once()
            assert proof.seats == []
        else:
            seat = await runner.adopt(proof, "claude", command)
            assert seat.surface.session_id == "seat-1"
            assert seat.surface.terminal_id == "terminal-1"
            assert proof.seats == [seat]
            proof.trace.register.assert_called_once_with(seat.surface)
            client.attach_terminal.assert_awaited_once_with(proof.locator.return_value)
            ws.close.assert_not_awaited()
        proof.empty.assert_not_awaited()
