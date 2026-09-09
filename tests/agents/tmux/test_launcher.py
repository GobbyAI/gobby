"""Large-command transport through the real shell and an isolated tmux server."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from gobby.agents.tmux.errors import TmuxSessionError
from gobby.agents.tmux.launcher import write_launcher
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig


@pytest.mark.unit
@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash", "/bin/zsh"])
def test_launcher_preserves_shell_quoting_exit_and_cleanup(shell: str, tmp_path: Path) -> None:
    if not Path(shell).exists():
        pytest.skip(f"{shell} unavailable")
    value = "quotes ' \" $() ` ; \\ \n café " * 1700
    output = tmp_path / "output"
    command = f"printf '%s' {shlex.quote(value)} > {shlex.quote(str(output))}; exit 23"
    launcher, invocation = write_launcher(command)
    try:
        assert launcher.stat().st_mode & 0o777 == 0o600
        result = subprocess.run([shell, "-c", invocation], capture_output=True, timeout=10)
        assert result.returncode == 23, result.stderr
        assert output.read_text() == value
        assert not launcher.exists()
    finally:
        launcher.unlink(missing_ok=True)


@pytest.mark.unit
async def test_failed_large_launch_cleans_private_files(tmp_path: Path) -> None:
    manager = TmuxSessionManager()
    with (
        patch("tempfile.tempdir", str(tmp_path)),
        patch.object(manager, "require_available"),
        patch.object(manager, "has_session", AsyncMock(return_value=False)),
        patch.object(manager, "_run", AsyncMock(return_value=(1, "", "origin failure"))),
        pytest.raises(TmuxSessionError, match="origin failure"),
    ):
        await manager.create_session(
            "large-failure", command=["echo", "x" * 40000], env={"API_KEY": "secret"}
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.integration
async def test_large_launch_in_isolated_tmux_preserves_arguments_and_environment(
    tmp_path: Path,
) -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux unavailable")
    manager = TmuxSessionManager(TmuxConfig(socket_name=f"gobby-launch-test-{uuid4().hex[:10]}"))
    value = "'\"; $() ` \\ \n café " * 2100
    output = tmp_path / "result"
    script = (
        'tmux set-hook -t "$TMUX_PANE" pane-died "wait-for -S launch-done"; '
        'printf "%s\\n%s" "$1" "$GOBBY_LAUNCH_TEST_VALUE" > "$2"; exit 19'
    )
    try:
        with patch("tempfile.tempdir", str(tmp_path)):
            await manager.create_session(
                "large",
                command=["/bin/sh", "-c", script, "launcher-test", value, str(output)],
                env={"GOBBY_LAUNCH_TEST_VALUE": value, "GOBBY_AGENT_API_TOKEN": "secret"},
            )
        async with asyncio.timeout(10):
            rc, _, stderr = await manager._run("wait-for", "launch-done")
            assert rc == 0, stderr
        rc, status, stderr = await manager._run(
            "display-message", "-p", "-t", "=large:", "#{pane_dead}:#{pane_dead_status}"
        )
        assert rc == 0, stderr
        assert status.strip() == "1:19"
        assert output.read_text() == f"{value}\n{value}"
        assert sorted(path.name for path in tmp_path.iterdir()) == ["result"]
    finally:
        await manager._run("kill-server")
