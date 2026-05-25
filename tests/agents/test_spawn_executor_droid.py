"""Droid-specific tests for agent spawn execution."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess  # nosec B404 # integration test launches local CLIs.
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import _copy_cli_hooks
from gobby.agents.spawn_executor import SpawnRequest, SpawnResult, execute_spawn
from gobby.utils.daemon_client import DaemonClient

pytestmark = pytest.mark.unit


def _droid_request(**overrides) -> SpawnRequest:
    values = {
        "prompt": "Test",
        "cwd": "/tmp/wt",
        "provider": "droid",
        "session_id": "sess",
        "run_id": "run",
        "parent_session_id": "parent",
        "project_id": "proj",
    }
    values.update(overrides)
    return SpawnRequest(**values)


class TestExecuteSpawnDroid:
    @pytest.mark.asyncio
    async def test_droid_provider_dispatches_to_droid_spawner(self) -> None:
        request = _droid_request()
        expected = SpawnResult(
            success=True,
            run_id="run",
            child_session_id="child",
            status="pending",
        )

        with patch(
            "gobby.agents.spawn_executor._spawn_droid_terminal",
            new_callable=AsyncMock,
        ) as mock_spawn:
            mock_spawn.return_value = expected
            result = await execute_spawn(request)

        assert result is expected
        mock_spawn.assert_awaited_once_with(request)

    @pytest.mark.asyncio
    async def test_droid_terminal_requires_session_manager(self) -> None:
        result = await execute_spawn(_droid_request())

        assert result.success is False
        assert "session_manager is required" in (result.error or "")

    @pytest.mark.asyncio
    async def test_droid_terminal_reports_missing_binary(self) -> None:
        request = _droid_request(session_manager=MagicMock())

        with (
            patch("gobby.agents.spawn_executor.shutil.which", return_value=None),
            patch("gobby.agents.spawn_executor.prepare_terminal_spawn") as mock_prepare,
        ):
            result = await execute_spawn(request)

        assert result.success is False
        assert "droid CLI not found in PATH" in (result.error or "")
        assert "docs/cli-integrations/droid.md" in (result.error or "")
        mock_prepare.assert_not_called()

    @pytest.mark.asyncio
    async def test_droid_terminal_builds_command_and_env(self) -> None:
        request = _droid_request(
            prompt="Fix it",
            agent_run_id="run-droid",
            session_manager=MagicMock(),
            machine_id="machine-1",
            model="claude-opus-4-7",
            effective_reasoning_effort="high",
            api_base="https://factory.example/v1",
            api_token="factory-token",
        )
        mock_prepare = MagicMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                agent_run_id="run-droid",
                env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
            )
        )
        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="tmux",
            tmux_session_name="agent-run-droid",
            tmux_socket_name="sock",
            tmux_socket_path="/tmp/sock",
        )

        with (
            patch("gobby.agents.spawn_executor.shutil.which", return_value="/usr/bin/droid"),
            patch("gobby.agents.spawn_executor.prepare_terminal_spawn", mock_prepare),
            patch("gobby.agents.spawn_executor.pre_approve_directory") as mock_pre_approve,
            patch("gobby.agents.spawn_executor.TmuxSpawner", return_value=mock_spawner),
        ):
            result = await execute_spawn(request)

        mock_prepare.assert_called_once()
        prepare_kwargs = mock_prepare.call_args.kwargs
        assert prepare_kwargs["source"] == "droid"
        assert prepare_kwargs["agent_run_id"] == "run-droid"
        assert prepare_kwargs["prompt"] == "Fix it"
        mock_pre_approve.assert_called_once_with("droid", "/tmp/wt")

        spawn_kwargs = mock_spawner.spawn.call_args.kwargs
        assert spawn_kwargs["cwd"] == "/tmp/wt"
        assert spawn_kwargs["command"] == [
            "droid",
            "exec",
            "--input-format",
            "stream-json",
            "--cwd",
            "/tmp/wt",
            "--model",
            "claude-opus-4-7",
            "--reasoning-effort",
            "high",
            "--auto",
            "high",
            "Fix it",
        ]
        assert "--worktree" not in spawn_kwargs["command"]
        assert "--session-id" not in spawn_kwargs["command"]
        assert spawn_kwargs["env"]["GOBBY_SESSION_ID"] == "gobby-sess-123"
        assert spawn_kwargs["env"]["GOBBY_MACHINE_ID"] == "machine-1"
        assert spawn_kwargs["env"]["FACTORY_API_KEY"] == "factory-token"
        assert spawn_kwargs["env"]["FACTORY_API_BASE_URL"] == "https://factory.example/v1"

        assert result.success is True
        assert result.run_id == "run-droid"
        assert result.child_session_id == "gobby-sess-123"
        assert result.tmux_session_name == "agent-run-droid"
        assert result.message == "Droid agent spawned in terminal with session gobby-sess-123"

    @pytest.mark.asyncio
    async def test_droid_terminal_spawn_failure(self) -> None:
        request = _droid_request(session_manager=MagicMock())
        mock_prepare = MagicMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                agent_run_id="run-droid",
                env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
            )
        )
        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(success=False, error="tmux failed")

        with (
            patch("gobby.agents.spawn_executor.shutil.which", return_value="/usr/bin/droid"),
            patch("gobby.agents.spawn_executor.prepare_terminal_spawn", mock_prepare),
            patch("gobby.agents.spawn_executor.pre_approve_directory"),
            patch("gobby.agents.spawn_executor.TmuxSpawner", return_value=mock_spawner),
        ):
            result = await execute_spawn(request)

        assert result.success is False
        assert result.child_session_id == "gobby-sess-123"
        assert result.error == "tmux failed"


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("droid") is None, reason="droid CLI not installed")
@pytest.mark.skipif(
    os.environ.get("GOBBY_RUN_DROID_HOOK_INTEGRATION") != "1",
    reason="set GOBBY_RUN_DROID_HOOK_INTEGRATION=1 to launch a live Droid/tmux hook test",
)
def test_droid_worktree_spawn_fires_pre_tool_use_against_gobby_daemon(
    tmp_path: Path,
) -> None:
    """Opt-in live check that Droid PreToolUse fires from an isolated worktree.

    The fallback hook copy in ``isolation.py`` writes Gobby-owned Droid hooks
    into the worktree-local Factory config. This harness launches real Droid
    under that worktree, verifies a PreToolUse sentinel hook ran, and confirms
    the Gobby daemon accepted at least one hook request.
    """
    if shutil.which("git") is None:
        pytest.skip("git is required to create the Droid hook integration worktree")

    client = DaemonClient(timeout=5.0)
    healthy, error = client.check_health()
    if not healthy:
        pytest.skip(f"Gobby daemon is not ready for Droid hook integration: {error}")

    worktree_path = _create_integration_worktree(tmp_path)
    asyncio.run(
        _copy_cli_hooks(
            source_path=str(tmp_path / "repo"),
            target_path=str(worktree_path),
            provider="droid",
        )
    )

    sentinel_path = tmp_path / "pretooluse-fired.txt"
    _prepend_pre_tool_use_sentinel(
        worktree_path / ".factory" / "hooks" / "hooks.json",
        sentinel_path,
    )

    before_hooks = _hooks_total(client)
    env = os.environ.copy()
    env["GOBBY_DROID_HOOK_SENTINEL"] = str(sentinel_path)

    result = subprocess.run(  # nosec B603 # opt-in integration against local Droid CLI.
        [
            "droid",
            "exec",
            "--input-format",
            "stream-json",
            "--cwd",
            str(worktree_path),
            "--auto",
            "high",
            "Use a shell command to list the files in the current directory, then stop.",
        ],
        cwd=worktree_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert _wait_for_path(sentinel_path), "Droid did not fire the PreToolUse sentinel hook"
    assert sentinel_path.read_text().strip() == "PreToolUse"
    assert _hooks_total(client) > before_hooks


def _create_integration_worktree(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _run_git(repo_path, "init")
    _run_git(repo_path, "config", "user.email", "droid-hook-test@example.invalid")
    _run_git(repo_path, "config", "user.name", "Droid Hook Test")
    (repo_path / "README.md").write_text("Droid hook integration\n")
    _run_git(repo_path, "add", "README.md")
    _run_git(repo_path, "commit", "-m", "initial")

    worktree_path = tmp_path / "worktree"
    _run_git(repo_path, "worktree", "add", "-b", "droid-hook-test", str(worktree_path))
    return worktree_path


def _run_git(cwd: Path, *args: str) -> None:
    result = subprocess.run(  # nosec B603, B607 - test invokes fixed local git command.
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _prepend_pre_tool_use_sentinel(hooks_file: Path, sentinel_path: Path) -> None:
    settings = json.loads(hooks_file.read_text())
    script = (
        "import os; "
        "from pathlib import Path; "
        "Path(os.environ['GOBBY_DROID_HOOK_SENTINEL']).write_text('PreToolUse\\n')"
    )
    sentinel_entry = {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
            }
        ],
    }
    settings["hooks"]["PreToolUse"].insert(0, sentinel_entry)
    hooks_file.write_text(json.dumps(settings, indent=2) + "\n")


def _hooks_total(client: DaemonClient) -> int:
    response = client.call_http_api("/api/metrics/current", method="GET", timeout=5.0)
    response.raise_for_status()
    counters = response.json().get("counters", {})
    return int(counters.get("hooks_total", {}).get("value", 0))


def _wait_for_path(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return path.exists()
