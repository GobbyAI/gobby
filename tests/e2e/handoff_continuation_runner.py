"""Handoff-only isolated runner; the admitted composer runner stays unchanged."""

import asyncio
import os
from contextlib import suppress
from pathlib import Path

import httpx

from gobby.agents.detection.matcher import CompiledManifest
from gobby.agents.idle_detector import IdleDetector, plain_text
from gobby.terminals.frame_client import FrameClient, FrameProtocolError
from gobby.terminals.host_client import HostEpochChangedError, HostNotAdoptedError
from gobby.terminals.host_protocol import frames_socket_path
from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_live import Frames, LiveProof, ProofWsSession, Seat
from tests.e2e.composer_proof_runner import main as composer_main
from tests.e2e.conftest import daemon_token
from tests.e2e.handoff_continuation_barrier import install_barrier
from tests.e2e.test_terminal_client_stack import _ws_create

# Rules a provider's detection manifest names for a screen that waits on the operator.
_PROMPT_RULES = (("trust_prompt", "trust_prompt"), ("approval_prompt", "approval_prompt"))
_FRAME_READ_SECONDS = 5
# What reading an unregistered seat's frame can raise: the proof's refusals, the frame and
# host clients' protocol, epoch and adoption errors (all RuntimeError), socket and decode
# failures, and the terminal readback's HTTP errors. Any of them means "unreadable".
_UNREADABLE_FRAME_ERRORS = (
    ProofRefused,
    FrameProtocolError,
    HostEpochChangedError,
    HostNotAdoptedError,
    RuntimeError,
    TimeoutError,
    OSError,
    ValueError,
    httpx.HTTPError,
)


def classify_frame(manifest: CompiledManifest | None, detector: IdleDetector, ansi: str) -> str:
    """Name what a seat's screen shows without keeping any of its text.

    The class is ``trust_prompt``, ``approval_prompt``, ``composer`` or ``unknown``. It is
    the only frame-derived value a registration timeout records.
    """
    text = plain_text(ansi)
    if manifest is not None:
        for rule, label in _PROMPT_RULES:
            if manifest.match_rule(rule, text).match is not None:
                return label
    if detector.composer_read(ansi).state != "unknown":
        return "composer"
    return "unknown"


async def unregistered_frame_class(proof: LiveProof, provider: str, terminal_id: str) -> str:
    """Classify the last frame of a seat that never bound a session.

    Returns ``unreadable`` when the native frame cannot be read; never raises.
    """
    client: FrameClient | None = None
    frames: Frames | None = None
    try:
        locator = proof.locator(await proof.terminal(terminal_id))
        if locator.host_socket is None:
            return "unreadable"
        socket_dir = Path(locator.host_socket).parent
        reader, writer = await asyncio.open_unix_connection(frames_socket_path(socket_dir))
        client = FrameClient(reader, writer)
        await client.handshake(locator, local_token=daemon_token(socket_dir))
        await client.attach_terminal(locator)
        frames = Frames(client)
        frame = await frames.wait_for(lambda _: True, timeout=_FRAME_READ_SECONDS)
        manifest = proof.detection_registry.for_provider(provider)
        return classify_frame(
            manifest, IdleDetector(proof.detection_registry, provider), frame.ansi
        )
    except _UNREADABLE_FRAME_ERRORS:
        return "unreadable"
    finally:
        with suppress(*_UNREADABLE_FRAME_ERRORS):
            if frames is not None:
                await frames.close()
            elif client is not None:
                await client.close()


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
        # Poll the authoritative row. The row binds after the SessionStart event, so waiting
        # for a later event let the seat's launch turn stage and deliver its handoff before
        # the proof trace owned the terminal.
        try:
            async with asyncio.timeout(90):
                item = await proof.terminal(terminal_id)
                while not item.get("session_id"):
                    await asyncio.sleep(0.2)
                    item = await proof.terminal(terminal_id)
        except TimeoutError:
            # Record what the seat stopped on, as a class only: no frame text or auth data.
            proof.evidence.append(
                {
                    "case": "registration_timeout",
                    "provider": provider,
                    "terminal_id": terminal_id,
                    "last_frame_class": await unregistered_frame_class(
                        proof, provider, terminal_id
                    ),
                }
            )
            raise
        locator = proof.locator(item)
        own, details = await proof.row_surface(provider, item)
        proof.scope.require_surface(own, own)
        if (
            own.terminal_id != terminal_id
            or details.get("source") != provider
            or details.get("project_id") != proof.scope.project_id
        ):
            raise ProofRefused("real provider registration mismatch")
        # Own the surface before the slower attach steps: the real seat runs its launch turn
        # at once, and the daemon's handoff writes are refused for an unregistered terminal.
        proof.trace.register(own)
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
