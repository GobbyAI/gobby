"""Test the test-side barrier without starting a daemon or provider CLI."""

import asyncio
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.mcp_proxy.tools.sessions import _terminal_compaction as compaction
from tests.e2e import test_handoff_continuation_e2e as handoff
from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_live import LiveProof, Seat
from tests.e2e.handoff_continuation_barrier import HandoffBarrier, install_barrier

pytestmark = pytest.mark.unit


@pytest.fixture
def barrier_socket() -> Iterator[Path]:
    # pytest's named tmp_path exceeds macOS's AF_UNIX limit.
    with tempfile.TemporaryDirectory(prefix="hb") as root:
        yield Path(root) / "hold.sock"


async def test_compact_cannot_submit_until_restart_judgment_releases_it(
    barrier_socket: Path,
) -> None:
    original = AsyncMock(return_value=(True, None, None))
    async with HandoffBarrier(barrier_socket) as barrier:
        with patch.object(compaction, "_submit_command", original), install_barrier(barrier.socket):
            submitted = asyncio.create_task(
                compaction._submit_command(
                    AsyncMock(),
                    "/compact",
                    "seat-1",
                    cli_source="claude",
                    composer_read=None,
                    verify_seconds=0,
                )
            )
            try:
                await barrier.wait_for("seat-1")
                await asyncio.sleep(0)  # Give a wrongly unblocked submission its next loop turn.
                original.assert_not_awaited()
                assert not submitted.done(), "compact escaped the restart judgment barrier"
                barrier.release()
                assert await submitted == (True, None, None)
                original.assert_awaited_once()
            finally:
                submitted.cancel()
                await asyncio.gather(submitted, return_exceptions=True)


@pytest.mark.parametrize("status", [200, 409])
async def test_restart_judgment_and_marker_check_precede_release(status: int) -> None:
    barrier = MagicMock(spec=HandoffBarrier)
    barrier.wait_for = AsyncMock()
    seat = MagicMock(spec=Seat)
    seat.surface = MagicMock()
    seat.surface.session_id = "seat-1"
    proof = MagicMock(spec=LiveProof)
    db = object()
    order: list[str] = []

    async def request_restart(candidate: LiveProof) -> httpx.Response:
        assert candidate is proof
        barrier.release.assert_not_called()
        barrier.wait_for.assert_awaited_once_with("seat-1")
        order.append("restart")
        return httpx.Response(status, json={"status": "handoff_pending"})

    def marker_live(candidate: object) -> bool:
        assert candidate is db
        barrier.release.assert_not_called()
        order.append("marker")
        return True

    with (
        patch.object(handoff, "_request_restart", request_restart),
        patch.object(handoff, "_handoff_staged", marker_live),
    ):
        if status == 200:
            with pytest.raises(AssertionError):
                await handoff._assert_restart_held(proof, seat, barrier, db)
            barrier.release.assert_not_called()
            assert order == ["restart"]
        else:
            await handoff._assert_restart_held(proof, seat, barrier, db)
            assert order == ["restart", "marker"]
            barrier.release.assert_called_once_with()


async def test_barrier_rejects_the_wrong_seat(barrier_socket: Path) -> None:
    async with HandoffBarrier(barrier_socket) as barrier:
        reader, writer = await asyncio.open_unix_connection(str(barrier.socket))
        try:
            writer.write(b'{"command":"/compact","session_id":"another-seat"}\n')
            await writer.drain()
            with pytest.raises(ProofRefused, match="different seat"):
                await barrier.wait_for("seat-1")
            barrier.release()
            assert await reader.readline() == b"release\n"
        finally:
            writer.close()
            await writer.wait_closed()
