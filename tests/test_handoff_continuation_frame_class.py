"""A registration timeout records only the class of the seat's last frame."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.agents.detection.matcher import CompiledManifest, compile_manifest
from gobby.agents.idle_detector import IdleDetector
from gobby.terminals.frame_client import FrameProtocolError
from gobby.terminals.host_client import HostEpochChangedError, HostNotAdoptedError
from gobby.terminals.host_protocol import frames_socket_path
from tests.agents.detection_test_support import BundledDetectionRegistry, detection_manifest
from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_live import LiveProof
from tests.e2e.handoff_continuation_runner import classify_frame, unregistered_frame_class

_FAILURES = [
    ProofRefused("native host binding outside private proof root"),
    FrameProtocolError("bad frame"),
    HostEpochChangedError("epoch moved"),
    HostNotAdoptedError(),
    RuntimeError("unexpected"),
    TimeoutError(),
    ConnectionRefusedError(),
    ValueError("bad json"),
    httpx.ConnectError("refused"),
]

pytestmark = pytest.mark.unit

_SECRET = "provider-secret-not-for-artifacts"


class _Synthetic:
    def __init__(self, manifest: CompiledManifest) -> None:
        self.manifest = manifest

    def for_provider(self, provider_id: str) -> CompiledManifest | None:
        return self.manifest


def test_trust_prompt_frame_is_named_without_its_text() -> None:
    manifest = compile_manifest(detection_manifest("probe", "PROBE"))
    detector = IdleDetector(_Synthetic(manifest), "probe")
    frame = f"\x1b[1m{_SECRET}\x1b[0m\nPROBE trust this folder?\n"
    assert classify_frame(manifest, detector, frame) == "trust_prompt"


def test_frame_without_a_prompt_or_composer_is_unknown() -> None:
    manifest = compile_manifest(detection_manifest("probe", "PROBE"))
    detector = IdleDetector(_Synthetic(manifest), "probe")
    assert classify_frame(manifest, detector, f"{_SECRET}\nloading\n") == "unknown"
    assert classify_frame(None, detector, f"{_SECRET}\n") == "unknown"


_RUNNER = "tests.e2e.handoff_continuation_runner"


def _locator(tmp_path: Path) -> MagicMock:
    return MagicMock(return_value=SimpleNamespace(host_socket=str(tmp_path / "h" / "f.sock")))


@pytest.mark.parametrize("failure", _FAILURES, ids=lambda exc: type(exc).__name__)
async def test_terminal_readback_failure_records_unreadable(
    tmp_path: Path, failure: Exception
) -> None:
    proof = LiveProof.__new__(LiveProof)
    with (
        patch.object(proof, "terminal", AsyncMock(side_effect=failure)),
        patch.object(proof, "locator", _locator(tmp_path)),
    ):
        assert await unregistered_frame_class(proof, "grok", "t") == "unreadable"


Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@asynccontextmanager
async def _frames_host(handler: Handler) -> AsyncIterator[Path]:
    """A real frames socket in the sandbox-writable default temp dir (short for AF_UNIX)."""
    with tempfile.TemporaryDirectory(prefix="fc") as root:
        host = Path(root) / "h"
        host.mkdir()
        server = await asyncio.start_unix_server(handler, path=str(frames_socket_path(host)))
        try:
            yield host
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.parametrize("failure", _FAILURES, ids=lambda exc: type(exc).__name__)
async def test_frame_client_failure_records_unreadable_and_closes(failure: Exception) -> None:
    # A real frames socket accepts the connection; only the frame handshake fails.
    connected = asyncio.Event()

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connected.set()
        writer.close()

    proof = LiveProof.__new__(LiveProof)
    client = SimpleNamespace(handshake=AsyncMock(side_effect=failure), close=AsyncMock())
    async with _frames_host(accept) as host:
        locator = SimpleNamespace(host_socket=str(host / "control.sock"))
        with (
            patch.object(proof, "terminal", AsyncMock(return_value={"id": "t"})),
            patch.object(proof, "locator", MagicMock(return_value=locator)),
            patch(f"{_RUNNER}.FrameClient", return_value=client),
            patch(f"{_RUNNER}.daemon_token", return_value="local"),
        ):
            assert await unregistered_frame_class(proof, "agy", "t") == "unreadable"
        # The classifier reached the seat's real frames socket before the handshake failed.
        await asyncio.wait_for(connected.wait(), 5)
    assert client.handshake.await_args is not None
    assert client.handshake.await_args.args == (locator,)
    assert client.handshake.await_args.kwargs == {"local_token": "local"}
    client.close.assert_awaited_once()


async def test_silent_host_is_bounded_and_its_connection_closed() -> None:
    """A host that accepts and never answers the handshake cannot stall the evidence."""
    closed = asyncio.Event()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # never answers; returns only once the classifier hangs up
        closed.set()
        writer.close()

    proof = LiveProof.__new__(LiveProof)
    loop = asyncio.get_running_loop()
    async with _frames_host(silent) as host:
        locator = SimpleNamespace(
            host_socket=str(host / "control.sock"),
            frame_host_epoch="epoch",
            host_terminal_id="t",
        )
        with (
            patch.object(proof, "terminal", AsyncMock(return_value={"id": "t"})),
            patch.object(proof, "locator", MagicMock(return_value=locator)),
            patch(f"{_RUNNER}.daemon_token", return_value="local"),
            patch(f"{_RUNNER}._FRAME_READ_SECONDS", 0.3),
        ):
            started = loop.time()
            assert await unregistered_frame_class(proof, "grok", "t") == "unreadable"
            elapsed = loop.time() - started
        await asyncio.wait_for(closed.wait(), 5)
    assert elapsed < 3, f"silent host held the diagnostic for {elapsed:.1f}s"


def test_a_visible_composer_is_named_composer() -> None:
    registry = BundledDetectionRegistry()
    detector = IdleDetector(registry, "claude")
    frame = "Welcome\n\n────────\n❯ \n────────\n"
    assert detector.composer_read(frame).state != "unknown", "fixture must show a composer"
    assert classify_frame(None, detector, frame) == "composer"
