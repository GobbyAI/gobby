"""Cancellable probes preserve the spawn.run result and error contracts."""

import asyncio
import contextlib
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from gobby.utils import spawn

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("cancel_during_cleanup", [False, True], ids=["timeout", "later-cancel"])
async def test_probe_fences_commands_and_preserves_cancellation_during_cleanup(
    tmp_path: Path, cancel_during_cleanup: bool
) -> None:
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    fenced = threading.Event()
    marker = tmp_path / "unexpected-command"

    def probe() -> None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            spawn.run([sys.executable, "-c", "import threading; threading.Event().wait(30)"])
        try:
            spawn.run(
                [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
            )
        except subprocess.TimeoutExpired:
            fenced.set()
        finally:
            cleanup_started.set()
            release_cleanup.wait(5)

    task = asyncio.create_task(spawn.run_cancellable_probe(probe, timeout=0.1))
    try:
        assert await asyncio.to_thread(cleanup_started.wait, 5)
        assert fenced.is_set()
        assert not marker.exists()
        assert not task.done()
        if cancel_during_cleanup:
            task.cancel()
        release_cleanup.set()
        if cancel_during_cleanup:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await task
    finally:
        release_cleanup.set()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await task


@pytest.mark.parametrize("text", [True, False])
async def test_probe_preserves_command_input_and_output(text: bool) -> None:
    payload = "proof" if text else b"proof"

    def probe() -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        command = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
        if text:
            return spawn.run(command, input="proof", capture_output=True, text=True, check=True)
        return spawn.run(
            command,
            input=b"proof",
            capture_output=True,
            text=False,
            check=True,
        )

    result = await spawn.run_cancellable_probe(probe, timeout=5)
    assert result.returncode == 0
    assert result.stdout == payload
    assert result.stderr == ("" if text else b"")


async def test_probe_preserves_failed_command_details() -> None:
    def probe() -> subprocess.CompletedProcess[str]:
        return spawn.run(
            [sys.executable, "-c", "import sys; print('refused', file=sys.stderr); sys.exit(7)"],
            capture_output=True,
            text=True,
            check=True,
        )

    with pytest.raises(subprocess.CalledProcessError) as raised:
        await spawn.run_cancellable_probe(probe, timeout=5)
    assert raised.value.returncode == 7
    assert raised.value.stderr == "refused\n"


async def test_probe_preserves_a_commands_own_timeout() -> None:
    def probe() -> subprocess.CompletedProcess[str]:
        return spawn.run(
            [
                sys.executable,
                "-c",
                "import threading; print('started', flush=True); threading.Event().wait(30)",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        await spawn.run_cancellable_probe(probe, timeout=5)
    assert raised.value.timeout == 2
    assert raised.value.stdout == b"started\n"
