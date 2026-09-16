"""Integration test for the environment a real tmux pane receives."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig

pytestmark = pytest.mark.integration


async def _wait_for_file(path: Path, timeout: float = 6) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


@pytest.mark.asyncio
async def test_pane_receives_the_callers_path_and_shell(tmp_path: Path) -> None:
    """tmux overwrites PATH and SHELL after applying ``-e``; the pane must still see ours."""
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux binary is not installed")

    socket_name = f"gobby-test-{uuid4().hex}"
    capture_path = tmp_path / "pane-env.txt"
    staged_path = tmp_path / "pane-env.tmp"
    caller_path = f"{tmp_path / 'caller-bin'}:/usr/bin:/bin"
    caller_shell = str(tmp_path / "caller-shell")
    command = (
        f'printf "%s\\n%s\\n" "$PATH" "$SHELL" > {shlex.quote(str(staged_path))} '
        f"&& mv {shlex.quote(str(staged_path))} {shlex.quote(str(capture_path))}"
    )

    manager = TmuxSessionManager(TmuxConfig(socket_name=socket_name))
    try:
        await manager.create_session(
            name=f"env-{uuid4().hex}",
            command=command,
            cwd=str(tmp_path),
            env={"PATH": caller_path, "SHELL": caller_shell},
        )
        pane_env = await _wait_for_file(capture_path)
    finally:
        subprocess.run(
            [tmux, "-L", socket_name, "kill-server"],
            check=False,
            capture_output=True,
            timeout=10,
        )

    assert pane_env.splitlines() == [caller_path, caller_shell]


@pytest.mark.asyncio
async def test_pane_command_runs_when_default_shell_cannot_parse_sh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user SHELL such as fish becomes tmux's default-shell; Gobby's sh must still run."""
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux binary is not installed")

    # tmux seeds default-shell from the server's SHELL. /usr/bin/false stands in for a
    # shell that cannot run Gobby's POSIX prefix: handed a lone string, nothing runs.
    monkeypatch.setenv("SHELL", "/usr/bin/false")
    socket_name = f"gobby-test-{uuid4().hex}"
    capture_path = tmp_path / "ran.txt"
    manager = TmuxSessionManager(TmuxConfig(socket_name=socket_name))
    try:
        await manager.create_session(
            name=f"sh-{uuid4().hex}",
            command=f"echo ran > {shlex.quote(str(capture_path))}",
            cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin"},
        )
        ran = await _wait_for_file(capture_path)
    finally:
        subprocess.run(
            [tmux, "-L", socket_name, "kill-server"],
            check=False,
            capture_output=True,
            timeout=10,
        )

    assert ran == "ran\n"
