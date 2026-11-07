"""Handoff-only isolated runner; the admitted composer runner stays unchanged."""

import asyncio
import os
from pathlib import Path

from gobby.agents.idle_detector import IdleDetector
from gobby.terminals.frame_client import FrameClient
from gobby.terminals.host_protocol import frames_socket_path
from tests.e2e.composer_proof import ProofRefused, Surface
from tests.e2e.composer_proof_live import Frames, LiveProof, ProofWsSession, Seat
from tests.e2e.composer_proof_runner import main as composer_main
from tests.e2e.conftest import daemon_token
from tests.e2e.handoff_continuation_barrier import install_barrier
from tests.e2e.test_terminal_client_stack import _ws_create


async def adopt(proof: LiveProof, provider: str, command: list[str]) -> Seat:
    """Register and attach a seat without waiting for it to go idle.

    A caller that must observe a transient window, such as a staged handoff that a later
    idle wait could outlast, reads the seat's own events itself instead of calling bind.
    """
    ws = ProofWsSession(proof.daemon)
    client: FrameClient | None = None
    adopted = False
    try:
        await ws.connect()
        proof.creation_uncertain = True
        created = await _ws_create(proof.daemon, command)
        if created.get("success") is not True or not created.get("terminal_id"):
            raise ProofRefused("proof provider create failed")
        terminal_id = str(created["terminal_id"])
        proof.created_terminal_ids.append(terminal_id)
        proof.creation_uncertain = False
        # Consume registration events, rereading the authoritative row after each.
        start = 0
        async with asyncio.timeout(90):
            item = await proof.terminal(terminal_id)
            while not item.get("session_id"):
                await ws.wait_for(
                    lambda event, baseline=start: event.get("type") == "session_event"
                    and event.get("project_id") == proof.scope.project_id
                    and event in ws.messages[baseline:],
                    timeout=90,
                    description="real provider registration",
                )
                start = len(ws.messages)
                item = await proof.terminal(terminal_id)
        locator = proof.locator(item)
        own = Surface(
            provider,
            str(item.get("session_id")),
            terminal_id,
            locator.frame_host_epoch,
            str(item.get("project_id")),
        )
        proof.scope.require_surface(own, own)
        details = await proof.call("gobby-sessions", "get_session", {"session_id": own.session_id})
        if details.get("source") != provider or details.get("project_id") != proof.scope.project_id:
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
            IdleDetector(proof.detection_registry, provider),
            locator,
        )
        proof.trace.register(own)
        proof.seats.append(seat)
        adopted = True
        return seat
    finally:
        if not adopted:
            if client is not None:
                await client.close()
            await ws.close()


def main() -> None:
    socket = os.environ.get("GOBBY_HANDOFF_BARRIER_SOCKET")
    if socket is None:
        composer_main()
    else:
        with install_barrier(Path(socket)):
            composer_main()


if __name__ == "__main__":
    main()
