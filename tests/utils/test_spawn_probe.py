"""Cancellable probes preserve the spawn.run result and error contracts."""

import subprocess
import sys

import pytest

from gobby.utils import spawn

pytestmark = pytest.mark.unit


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
            timeout=0.2,
        )

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        await spawn.run_cancellable_probe(probe, timeout=5)
    assert raised.value.timeout == 0.2
    assert raised.value.stdout == b"started\n"
