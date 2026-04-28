"""Droid-specific tests for agent spawn execution."""

from __future__ import annotations

import os
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.spawn_executor import SpawnRequest, SpawnResult, execute_spawn

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
def test_droid_worktree_spawn_inherits_global_hooks() -> None:
    """Live verification placeholder for Droid global hook inheritance.

    The implementation intentionally does not copy hooks into isolation roots:
    Droid reads user-global ``~/.factory/hooks/hooks.json`` independently of
    ``--cwd``. This test is opt-in because it launches a real Droid session,
    depends on a running Gobby daemon with Droid hooks installed, and may call
    external model APIs.
    """
    pytest.skip("live Droid hook integration requires an explicit local harness run")
