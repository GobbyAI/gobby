"""Integration tests for tmux literal text injection."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from gobby.agents.tmux.text_injection import send_literal_text_to_tmux_target

pytestmark = pytest.mark.integration


async def _run(*command: str, timeout: float = 5) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stdout, stderr


async def _wait_for_file(path: Path, timeout: float = 6) -> bytes:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return path.read_bytes()
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


@pytest.mark.asyncio
async def test_trailing_newline_waits_past_paste_suppression_window(tmp_path: Path) -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux binary is not installed")

    socket_name = f"gobby-test-{uuid4().hex}"
    session_name = f"paste-{uuid4().hex}"
    ready_signal = f"ready-{uuid4().hex}"
    capture_path = tmp_path / "paste.bin"
    reader_path = tmp_path / "reader.py"
    reader_path.write_text(
        """
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import termios
import time
import tty

fd = sys.stdin.fileno()
original = termios.tcgetattr(fd)
data = bytearray()
paste_end_at = None
enter_at = None

try:
    tty.setraw(fd)
    sys.stdout.write("\\033[?2004h")
    sys.stdout.flush()
    subprocess.run(["tmux", "-L", sys.argv[2], "wait-for", "-S", sys.argv[3]], check=False)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.1)
        if not readable:
            continue
        chunk = os.read(fd, 1)
        if not chunk:
            break
        received_at = time.monotonic()
        data.extend(chunk)
        paste_end = data.find(b"\\x1b[201~")
        if paste_end_at is None and paste_end >= 0:
            paste_end_at = received_at
        if paste_end >= 0 and len(data) > paste_end + len(b"\\x1b[201~"):
            enter_at = received_at
            break
finally:
    sys.stdout.write("\\033[?2004l")
    sys.stdout.flush()
    termios.tcsetattr(fd, termios.TCSADRAIN, original)
    temporary_path = f"{sys.argv[1]}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as output:
        json.dump(
            {
                "data_hex": data.hex(),
                "paste_end_at": paste_end_at,
                "enter_at": enter_at,
            },
            output,
        )
    os.replace(temporary_path, sys.argv[1])
""",
        encoding="utf-8",
    )

    tmux_cmd = ("tmux", "-L", socket_name, "-f", "/dev/null")
    try:
        returncode, _, stderr = await _run(
            *tmux_cmd,
            "new-session",
            "-d",
            "-s",
            session_name,
            "python",
            str(reader_path),
            str(capture_path),
            socket_name,
            ready_signal,
        )
        assert returncode == 0, stderr.decode()

        returncode, _, stderr = await _run(*tmux_cmd, "wait-for", ready_signal)
        assert returncode == 0, stderr.decode()

        await send_literal_text_to_tmux_target(
            f"{session_name}:0.0",
            "alpha\nbeta\n",
            tmux_cmd=tmux_cmd,
        )

        captured = json.loads((await _wait_for_file(capture_path)).decode())
        data = bytes.fromhex(captured["data_hex"])
        paste = b"\x1b[200~alpha\rbeta\x1b[201~"
        assert data == paste + b"\r"

        paste_end_at = captured["paste_end_at"]
        enter_at = captured["enter_at"]
        assert paste_end_at is not None
        assert enter_at is not None
        assert enter_at - paste_end_at >= 0.12
    finally:
        await _run(*tmux_cmd, "kill-server")
