"""
Tests for SpawnExecutor unified spawn dispatch.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.constants import UV_CACHE_DIR
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn_executor import (
    SpawnRequest,
    SpawnResult,
    _apply_extra_env,
    execute_spawn,
)

pytestmark = pytest.mark.unit


class TestSpawnRequest:
    """Tests for SpawnRequest dataclass."""

    def test_spawn_request_fields(self) -> None:
        """Test SpawnRequest has all required fields."""
        request = SpawnRequest(
            prompt="Test prompt",
            cwd="/path/to/project",
            provider="claude",
            session_id="session-123",
            run_id="run-456",
            parent_session_id="parent-789",
            project_id="proj-abc",
        )

        assert request.prompt == "Test prompt"
        assert request.cwd == "/path/to/project"
        assert request.provider == "claude"
        assert request.session_id == "session-123"
        assert request.run_id == "run-456"
        assert request.parent_session_id == "parent-789"
        assert request.project_id == "proj-abc"

    def test_spawn_request_optional_fields(self) -> None:
        """Test SpawnRequest optional fields have defaults."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
        )

        assert request.workflow is None
        assert request.worktree_id is None
        assert request.clone_id is None
        assert request.agent_depth == 0
        assert request.max_agent_depth == 5

    def test_spawn_request_sandbox_fields_default_to_none(self) -> None:
        """Test SpawnRequest sandbox fields default to None."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
        )

        assert request.sandbox_config is None
        assert request.sandbox_args is None
        assert request.sandbox_env is None

    def test_spawn_request_accepts_sandbox_fields(self) -> None:
        """Test SpawnRequest accepts sandbox configuration."""
        sandbox_config = SandboxConfig(enabled=True, mode="restrictive")
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            sandbox_config=sandbox_config,
            sandbox_args=["--settings", '{"sandbox":{"enabled":true}}'],
            sandbox_env={"SEATBELT_PROFILE": "restrictive-closed"},
        )

        assert request.sandbox_config is not None
        assert request.sandbox_config.enabled is True
        assert request.sandbox_config.mode == "restrictive"
        assert request.sandbox_args == ["--settings", '{"sandbox":{"enabled":true}}']
        assert request.sandbox_env == {"SEATBELT_PROFILE": "restrictive-closed"}

    def test_spawn_request_api_base_defaults_to_none(self) -> None:
        """Test SpawnRequest api_base and api_token default to None."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
        )

        assert request.api_base is None
        assert request.api_token is None

    def test_spawn_request_accepts_api_base_and_token(self) -> None:
        """Test SpawnRequest accepts api_base and api_token for local models."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            api_base="http://localhost:1234/v1",
            api_token="sk-local",
        )

        assert request.api_base == "http://localhost:1234/v1"
        assert request.api_token == "sk-local"


class TestSpawnResult:
    """Tests for SpawnResult dataclass."""

    def test_spawn_result_success(self) -> None:
        """Test successful SpawnResult."""
        result = SpawnResult(
            success=True,
            run_id="run-123",
            child_session_id="child-456",
            status="pending",
            pid=12345,
            terminal_type="ghostty",
        )

        assert result.success is True
        assert result.run_id == "run-123"
        assert result.child_session_id == "child-456"
        assert result.status == "pending"
        assert result.pid == 12345
        assert result.terminal_type == "ghostty"

    def test_spawn_result_failure(self) -> None:
        """Test failed SpawnResult."""
        result = SpawnResult(
            success=False,
            run_id="run-123",
            child_session_id="child-456",
            status="failed",
            error="Failed to spawn process",
        )

        assert result.success is False
        assert result.error == "Failed to spawn process"

    def test_spawn_result_optional_fields(self) -> None:
        """Test SpawnResult optional fields have defaults."""
        result = SpawnResult(
            success=True,
            run_id="run",
            child_session_id="child",
            status="pending",
        )

        assert result.pid is None
        assert result.terminal_type is None
        assert result.error is None
        assert result.message is None


class TestExecuteSpawn:
    """Tests for execute_spawn function."""

    @pytest.mark.asyncio
    async def test_terminal_mode_calls_terminal_spawner(self):
        """Test that terminal mode dispatches to TerminalSpawner."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
            machine_id="test-machine",
        )

        # Mock prepare_terminal_spawn
        mock_spawn_context = MagicMock()
        mock_spawn_context.session_id = "child-session-id"
        mock_spawn_context.agent_run_id = "run-123"
        mock_spawn_context.env_vars = {
            "GOBBY_SESSION_ID": "child-session-id",
            UV_CACHE_DIR: "/tmp/gobby/uv-cache/child-session-id",
        }

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="ghostty",
            message="Spawned successfully",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

            mock_spawner.spawn.assert_called_once()
            assert result.success is True
            assert result.pid == 12345
            assert result.child_session_id == "child-session-id"

    @pytest.mark.asyncio
    async def test_spawn_failure_propagates_error(self):
        """Test that spawn failure returns error in result."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
            machine_id="test-machine",
        )

        mock_spawn_context = MagicMock()
        mock_spawn_context.session_id = "child-session-id"
        mock_spawn_context.agent_run_id = "run-123"
        mock_spawn_context.env_vars = {
            "GOBBY_SESSION_ID": "child-session-id",
            UV_CACHE_DIR: "/tmp/gobby/uv-cache/child-session-id",
        }

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=False,
            error="Terminal not found",
            message="Failed to spawn",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

            assert result.success is False
            assert "Terminal not found" in (result.error or result.message or "")

    @pytest.mark.asyncio
    async def test_spawn_passes_workflow_to_spawner(self):
        """Test that workflow is passed to prepare_terminal_spawn."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            workflow="auto-task",
            session_manager=mock_session_manager,
            machine_id="test-machine",
        )

        mock_spawn_context = MagicMock()
        mock_spawn_context.session_id = "child-session-id"
        mock_spawn_context.agent_run_id = "run-123"
        mock_spawn_context.env_vars = {
            "GOBBY_SESSION_ID": "child-session-id",
            UV_CACHE_DIR: "/tmp/gobby/uv-cache/child-session-id",
        }

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="ghostty",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ) as mock_prepare,
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            await execute_spawn(request)

            # Workflow is passed to prepare_terminal_spawn, not directly to spawner
            mock_prepare.assert_called_once()
            assert mock_prepare.call_count == 1
            assert mock_prepare.call_args is not None
            call_kwargs = mock_prepare.call_args.kwargs
            assert call_kwargs.get("workflow_name") == "auto-task"

    @pytest.mark.asyncio
    async def test_gemini_terminal_calls_prepare_terminal_spawn(self):
        """Test that provider='gemini' with mode='interactive' uses direct spawn with env vars.

        Gemini now uses direct spawn with GOBBY_SESSION_ID env var passed to the terminal.
        Session linkage happens when Gemini's hook dispatcher sends the env vars to daemon.
        """
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="gemini",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
        )

        mock_prepare = MagicMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                agent_run_id="run-abc123",
                env_vars={
                    "GOBBY_SESSION_ID": "gobby-sess-123",
                    UV_CACHE_DIR: "/tmp/gobby/uv-cache/gobby-sess-123",
                },
            )
        )

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                mock_prepare,
            ),
            patch(
                "gobby.agents.spawn_executor.build_cli_command",
                return_value=(["gemini", "--approval-mode", "yolo", "-i", "prompt"], {}),
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

            mock_prepare.assert_called_once()
            mock_spawner.spawn.assert_called_once()
            # Env vars ARE passed to spawn now (for hook dispatcher to read)
            call_kwargs = mock_spawner.spawn.call_args.kwargs
            assert call_kwargs.get("env") is not None
            assert "GOBBY_SESSION_ID" in call_kwargs["env"]
            assert result.success is True
            assert result.child_session_id == "gobby-sess-123"
            assert result.pid == 12345

    @pytest.mark.asyncio
    async def test_codex_terminal_direct_spawn(self):
        """Codex spawns directly (no preflight); command is `codex ...`, never `codex resume ...`."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="codex",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            project_path="/main/repo",
            agent_run_id="run-abc123def456",
            session_manager=mock_session_manager,
        )

        call_order: list[str] = []
        spawn_context = MagicMock(
            session_id="gobby-sess-123",
            agent_run_id="run-abc123def456",
            env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
        )
        mock_prepare = MagicMock(
            side_effect=lambda **_kwargs: call_order.append("prepare") or spawn_context
        )

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="tmux",
            tmux_session_name="agent-run-abc123def456",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                mock_prepare,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
            patch("gobby.agents.spawn_executor.pre_approve_directory") as mock_preapprove,
        ):
            mock_preapprove.side_effect = lambda *_args, **_kwargs: call_order.append("preapprove")
            result = await execute_spawn(request)

            # prepare_terminal_spawn must be called with source='codex' and the
            # caller's agent_run_id threaded through unchanged.
            mock_preapprove.assert_called_once_with("codex", "/path")
            mock_prepare.assert_called_once()
            assert call_order == ["prepare", "preapprove"]
            call_kwargs = mock_prepare.call_args.kwargs
            assert call_kwargs["source"] == "codex"
            assert call_kwargs["agent_run_id"] == "run-abc123def456"

            # Command starts with `codex` and never invokes `resume`.
            spawn_kwargs = mock_spawner.spawn.call_args.kwargs
            command = spawn_kwargs["command"]
            assert command[0] == "codex"
            assert "resume" not in command
            assert command[1:3] == ["--ask-for-approval", "never"]
            assert command[3:5] == ["--disable", "guardian_approval"]
            assert 'mcp_servers.gobby.command="uv"' in command
            assert (
                'mcp_servers.gobby.args=["run","--project","/main/repo","gobby","mcp-server"]'
                in command
            )
            assert "mcp_servers.gobby.startup_timeout_sec=120" in command
            assert "--full-auto" not in command

            # Env is passed to the tmux spawner so the SessionStart hook can
            # late-link via GOBBY_SESSION_ID.
            assert spawn_kwargs.get("env") is not None
            assert spawn_kwargs["env"].get("GOBBY_SESSION_ID") == "gobby-sess-123"

            # Codex gets its Gobby session id from env/hooks, not prompt text.
            prompt_arg = command[-1]
            assert prompt_arg == request.prompt
            assert "gobby-sess-123" not in prompt_arg
            assert "Your Gobby session_id is" not in prompt_arg

            # SpawnResult.run_id is the caller-minted id (no fabricated
            # `codex-xxxxxxxx` substitute).
            assert result.success is True
            assert result.run_id == "run-abc123def456"
            assert result.child_session_id == "gobby-sess-123"
            assert result.codex_session_id is None  # late-linked via SessionStart hook

    @pytest.mark.asyncio
    async def test_codex_preapprove_runs_after_command_and_env_setup(self) -> None:
        """Workspace trust is seeded after Codex command and environment setup."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="codex",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            agent_run_id="run-abc123def456",
            session_manager=MagicMock(),
        )
        call_order: list[str] = []
        spawn_context = MagicMock(
            session_id="gobby-sess-123",
            agent_run_id="run-abc123def456",
            env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
        )
        mock_spawner = MagicMock()
        mock_spawner.spawn.side_effect = lambda **_kwargs: call_order.append("spawn") or MagicMock(
            success=True, pid=12345, terminal_type="tmux"
        )

        def fake_apply_extra_env(_env: dict[str, str], _request: SpawnRequest) -> None:
            call_order.append("env")

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                side_effect=lambda **_kwargs: call_order.append("prepare") or spawn_context,
            ),
            patch(
                "gobby.agents.spawn_executor.build_cli_command",
                side_effect=lambda **_kwargs: call_order.append("command")
                or (["codex", "Test"], {}),
            ),
            patch("gobby.agents.spawn_executor._apply_extra_env", fake_apply_extra_env),
            patch("gobby.agents.spawn_executor.TmuxSpawner", return_value=mock_spawner),
            patch(
                "gobby.agents.spawn_executor.pre_approve_directory",
                side_effect=lambda *_args, **_kwargs: call_order.append("preapprove"),
            ),
        ):
            result = await execute_spawn(request)

        assert result.success is True
        assert call_order == ["prepare", "command", "env", "preapprove", "spawn"]

    @pytest.mark.asyncio
    async def test_codex_terminal_spawn_with_sandbox_config(self) -> None:
        """Test that Codex terminal spawn applies sandbox flags."""
        sandbox_config = SandboxConfig(enabled=True, mode="permissive")
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="codex",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
            sandbox_config=sandbox_config,
        )

        mock_prepare = MagicMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                agent_run_id="run-xyz",
                env_vars={
                    "GOBBY_SESSION_ID": "gobby-sess-123",
                    UV_CACHE_DIR: "/tmp/gobby/uv-cache/gobby-sess-123",
                },
            )
        )

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="tmux",
            tmux_session_name="agent-run-xyz",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                mock_prepare,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
            patch("gobby.agents.spawn_executor.pre_approve_directory") as mock_preapprove,
        ):
            result = await execute_spawn(request)

            mock_preapprove.assert_called_once_with("codex", "/path")
            command = mock_spawner.spawn.call_args.kwargs["command"]
            assert "--ask-for-approval" in command
            assert command[command.index("--ask-for-approval") + 1] == "never"
            assert command[command.index("--disable") + 1] == "guardian_approval"
            assert "--sandbox" in command
            assert "workspace-write" in command
            assert "-c" in command
            assert "sandbox_workspace_write.network_access=true" in command
            assert "--add-dir" in command
            assert command[command.index("--add-dir") + 1] == "/tmp/gobby/uv-cache/gobby-sess-123"
            assert "--full-auto" not in command
            prompt_arg = command[-1]
            # Sandbox args must appear before the final prompt argv entry, which
            # is the raw Codex prompt.
            assert prompt_arg == request.prompt
            assert request.prompt in prompt_arg
            assert command.index("--ask-for-approval") < command.index("--sandbox")
            assert command.index("--disable") < command.index("--sandbox")
            assert command.index("sandbox_workspace_write.network_access=true") < command.index(
                prompt_arg
            )
            assert command.index("--sandbox") < command.index(prompt_arg)
            assert result.success is True

    @pytest.mark.asyncio
    async def test_claude_terminal_requires_session_manager(self):
        """Test that Claude spawn requires session_manager."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            # No session_manager provided
        )

        result = await execute_spawn(request)

        assert result.success is False
        assert "session_manager is required" in (result.error or "")

    @pytest.mark.asyncio
    async def test_gemini_terminal_requires_session_manager(self):
        """Test that Gemini spawn requires session_manager for preflight."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="gemini",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            # No session_manager provided
        )

        result = await execute_spawn(request)

        assert result.success is False
        assert "session_manager is required" in (result.error or "")

    @pytest.mark.asyncio
    async def test_gemini_terminal_spawn_failure_propagates_error(self):
        """Test that Gemini spawn failure is properly propagated to SpawnResult."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="gemini",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
        )

        mock_prepare = MagicMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                agent_run_id="run-abc123",
                env_vars={
                    "GOBBY_SESSION_ID": "gobby-sess-123",
                    UV_CACHE_DIR: "/tmp/gobby/uv-cache/gobby-sess-123",
                },
            )
        )

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=False,
            error="Terminal not found",
            message=None,
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                mock_prepare,
            ),
            patch(
                "gobby.agents.spawn_executor.build_cli_command",
                return_value=(["gemini", "--approval-mode", "yolo", "-i", "prompt"], {}),
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

            assert result.success is False
            assert "Terminal not found" in (result.error or "")

    @pytest.mark.asyncio
    async def test_grok_terminal_spawn_constructs_headless_command(self):
        """Grok spawn uses the documented single-shot command and hook env linkage."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="grok",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
            model="grok-build",
            effective_reasoning_effort="high",
        )

        mock_prepare = MagicMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                agent_run_id="run-grok123",
                env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
            )
        )
        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="tmux",
            tmux_session_name="agent-run-grok123",
        )

        with (
            patch("gobby.agents.spawn_executor.prepare_terminal_spawn", mock_prepare),
            patch("gobby.agents.spawn_executor.TmuxSpawner", return_value=mock_spawner),
            patch("gobby.agents.spawn_executor.pre_approve_directory") as mock_preapprove,
        ):
            result = await execute_spawn(request)

        mock_prepare.assert_called_once()
        call_kwargs = mock_prepare.call_args.kwargs
        assert call_kwargs["source"] == "grok"
        mock_preapprove.assert_called_once_with("grok", "/path")

        spawn_kwargs = mock_spawner.spawn.call_args.kwargs
        assert spawn_kwargs["cwd"] == "/path"
        assert spawn_kwargs["env"]["GOBBY_SESSION_ID"] == "gobby-sess-123"
        assert spawn_kwargs["command"] == [
            "grok",
            "--always-approve",
            "--no-alt-screen",
            "--cwd",
            "/path",
            "--model",
            "grok-build",
            "--reasoning-effort",
            "high",
            "--single",
            "Test",
        ]
        assert result.success is True
        assert result.run_id == "run-grok123"
        assert result.child_session_id == "gobby-sess-123"

    @pytest.mark.asyncio
    async def test_grok_terminal_spawn_applies_sandbox_config(self):
        """Grok spawn passes built-in sandbox profile flags."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="grok",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=MagicMock(),
            sandbox_config=SandboxConfig(enabled=True, mode="restrictive"),
        )
        mock_prepare = MagicMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                agent_run_id="run-grok123",
                env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
            )
        )
        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(success=True, pid=12345, terminal_type="tmux")

        with (
            patch("gobby.agents.spawn_executor.prepare_terminal_spawn", mock_prepare),
            patch("gobby.agents.spawn_executor.TmuxSpawner", return_value=mock_spawner),
            patch("gobby.agents.spawn_executor.pre_approve_directory"),
        ):
            result = await execute_spawn(request)

        command = mock_spawner.spawn.call_args.kwargs["command"]
        assert "--sandbox" in command
        assert "strict" in command
        assert command.index("--sandbox") < command.index("Test")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_agy_spawn_rejects_unavailable_provider(self):
        """AGY is visible but explicitly unavailable for agent spawning."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="agy",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
        )

        result = await execute_spawn(request)

        assert result.success is False
        assert result.child_session_id is None
        assert "machine transport" in (result.error or "")


class TestExecuteSpawnSandbox:
    """Integration tests for sandbox configuration in spawn flow."""

    @pytest.mark.asyncio
    async def test_terminal_spawn_passes_sandbox_config_to_spawner(self) -> None:
        """Test that sandbox_config is resolved and passed to TerminalSpawner."""
        sandbox_config = SandboxConfig(enabled=True, mode="permissive")
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test with sandbox",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            sandbox_config=sandbox_config,
            session_manager=mock_session_manager,
            machine_id="test-machine",
        )

        mock_spawn_context = MagicMock()
        mock_spawn_context.session_id = "child-session-id"
        mock_spawn_context.agent_run_id = "run-123"
        mock_spawn_context.env_vars = {
            "GOBBY_SESSION_ID": "child-session-id",
            UV_CACHE_DIR: "/tmp/gobby/uv-cache/child-session-id",
        }

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="ghostty",
        )

        # Mock the sandbox resolver (imported locally in _spawn_claude_terminal)
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = (
            ["--settings", '{"sandbox":{"enabled":true}}'],
            {"SEATBELT_PROFILE": "permissive-open"},
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
            patch(
                "gobby.agents.sandbox.ClaudeSandboxResolver",
                return_value=mock_resolver,
            ),
            patch("gobby.agents.spawn_executor.pre_approve_directory"),
        ):
            result = await execute_spawn(request)

            # Verify sandbox was resolved and env vars passed to spawn
            mock_resolver.resolve.assert_called_once()
            resolved_config = mock_resolver.resolve.call_args.args[0]
            assert "/tmp/gobby/uv-cache/child-session-id" in resolved_config.extra_write_paths
            mock_spawner.spawn.assert_called_once()
            call_kwargs = mock_spawner.spawn.call_args.kwargs
            assert "env" in call_kwargs
            assert "SEATBELT_PROFILE" in call_kwargs["env"]
            assert call_kwargs["env"][UV_CACHE_DIR] == "/tmp/gobby/uv-cache/child-session-id"
            # Command should include sandbox args
            command = call_kwargs.get("command")
            assert "--dangerously-skip-permissions" in command
            assert "--settings" in command
            assert result.success is True

    @pytest.mark.asyncio
    async def test_terminal_spawn_without_sandbox_passes_none(self) -> None:
        """Test that spawn without sandbox doesn't add sandbox env vars."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test without sandbox",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
            machine_id="test-machine",
            # No sandbox_config specified
        )

        mock_spawn_context = MagicMock()
        mock_spawn_context.session_id = "child-session-id"
        mock_spawn_context.agent_run_id = "run-123"
        mock_spawn_context.env_vars = {"GOBBY_SESSION_ID": "child-session-id"}

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="ghostty",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

            mock_spawner.spawn.assert_called_once()
            call_kwargs = mock_spawner.spawn.call_args.kwargs
            # Env should only have gobby session vars, no sandbox vars
            assert "SEATBELT_PROFILE" not in call_kwargs.get("env", {})
            assert result.success is True

    @pytest.mark.asyncio
    async def test_sandbox_disabled_explicitly_passed(self) -> None:
        """Test that explicitly disabled sandbox doesn't add sandbox env vars."""
        sandbox_config = SandboxConfig(enabled=False)
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            sandbox_config=sandbox_config,
            session_manager=mock_session_manager,
            machine_id="test-machine",
        )

        mock_spawn_context = MagicMock()
        mock_spawn_context.session_id = "child-session-id"
        mock_spawn_context.agent_run_id = "run-123"
        mock_spawn_context.env_vars = {"GOBBY_SESSION_ID": "child-session-id"}

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="ghostty",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

            mock_spawner.spawn.assert_called_once()
            call_kwargs = mock_spawner.spawn.call_args.kwargs
            # Env should only have gobby session vars, no sandbox vars (sandbox disabled)
            assert "SEATBELT_PROFILE" not in call_kwargs.get("env", {})
            assert result.success is True

    @pytest.mark.asyncio
    async def test_gemini_terminal_spawn_with_sandbox_config(self) -> None:
        """Test that Gemini terminal spawn applies sandbox config correctly."""
        sandbox_config = SandboxConfig(
            enabled=True,
            mode="permissive",
            extra_write_paths=["/tmp/gobby-gemini-git"],
        )
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test with sandbox",
            cwd="/path",
            provider="gemini",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
            sandbox_config=sandbox_config,
        )

        mock_prepare = MagicMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                agent_run_id="run-abc123",
                env_vars={
                    "GOBBY_SESSION_ID": "gobby-sess-123",
                    UV_CACHE_DIR: "/tmp/gobby/uv-cache/gobby-sess-123",
                },
            )
        )

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                mock_prepare,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

            mock_spawner.spawn.assert_called_once()
            call_kwargs = mock_spawner.spawn.call_args.kwargs
            # Env should include both Gobby session vars and sandbox vars
            assert call_kwargs.get("env") is not None
            assert "GOBBY_SESSION_ID" in call_kwargs["env"]
            assert "SEATBELT_PROFILE" in call_kwargs["env"]
            assert call_kwargs["env"]["SEATBELT_PROFILE"] == "permissive-open"
            # Command should include -s flag (passed as keyword arg)
            command = call_kwargs.get("command")
            assert command is not None
            assert "--approval-mode" in command
            assert "yolo" in command
            assert "-s" in command
            assert "--include-directories" in command
            assert command[command.index("--include-directories") + 1] == str(
                Path("/tmp/gobby-gemini-git").resolve(strict=False)
            )
            assert str(Path("/tmp/gobby/uv-cache/gobby-sess-123").resolve(strict=False)) in command
            assert command[-1] == request.prompt
            assert command.index("-s") < len(command) - 1
            assert command.index("--include-directories") < len(command) - 1
            assert result.success is True

    @pytest.mark.asyncio
    async def test_qwen_terminal_spawn_uses_qwen_resolver_for_sandbox_config(self) -> None:
        """Qwen uses its own resolver and passes sandbox args before the prompt."""
        sandbox_config = SandboxConfig(enabled=True, mode="permissive")
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test with sandbox",
            cwd="/path",
            provider="qwen",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
            sandbox_config=sandbox_config,
        )

        mock_prepare = MagicMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                agent_run_id="run-abc123",
                env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
            )
        )
        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(success=True, pid=12345)
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = (
            ["-s", "--include-directories", "/tmp/qwen-git"],
            {"SEATBELT_PROFILE": "permissive-open"},
        )

        with (
            patch("gobby.agents.spawn_executor.prepare_terminal_spawn", mock_prepare),
            patch(
                "gobby.agents.spawn_executor.QwenSandboxResolver", return_value=mock_resolver
            ) as mock_resolver_class,
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

        mock_resolver_class.assert_called_once()
        mock_resolver.resolve.assert_called_once()
        command = mock_spawner.spawn.call_args.kwargs["command"]
        assert command[0] == "qwen"
        assert command[-1] == request.prompt
        assert command.index("-s") < len(command) - 1
        assert command.index("--include-directories") < len(command) - 1
        assert mock_spawner.spawn.call_args.kwargs["env"]["SEATBELT_PROFILE"] == "permissive-open"
        assert result.success is True


class TestExecuteSpawnErrorPaths:
    """Tests for spawn execution error paths and edge cases."""

    @pytest.mark.asyncio
    async def test_codex_terminal_requires_session_manager(self) -> None:
        """Test that Codex terminal spawn requires session_manager."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="codex",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            # No session_manager
        )

        result = await execute_spawn(request)

        assert result.success is False
        assert "session_manager is required" in (result.error or "")

    @pytest.mark.asyncio
    async def test_codex_terminal_spawn_failure(self) -> None:
        """Codex terminal returns failure when tmux spawn fails."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="codex",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
        )

        mock_prepare = MagicMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                agent_run_id="run-xyz",
                env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
            )
        )

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=False,
            error="tmux failed",
            message=None,
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                mock_prepare,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
            patch("gobby.agents.spawn_executor.pre_approve_directory") as mock_preapprove,
        ):
            result = await execute_spawn(request)

        mock_preapprove.assert_called_once_with("codex", "/path")
        assert result.success is False
        assert "tmux failed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_claude_terminal_passes_machine_id_env(self) -> None:
        """Test that machine_id is passed as GOBBY_MACHINE_ID env var."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
            machine_id="machine-xyz",
        )

        mock_spawn_context = MagicMock()
        mock_spawn_context.session_id = "child-session-id"
        mock_spawn_context.agent_run_id = "run-123"
        mock_spawn_context.env_vars = {"GOBBY_SESSION_ID": "child-session-id"}

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="tmux",
            tmux_session_name="gobby-test",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

        assert result.success is True
        call_kwargs = mock_spawner.spawn.call_args.kwargs
        assert call_kwargs["env"]["GOBBY_MACHINE_ID"] == "machine-xyz"

    @pytest.mark.asyncio
    async def test_claude_terminal_tmux_session_name_in_result(self) -> None:
        """Test that tmux_session_name is propagated to SpawnResult."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
            machine_id="m",
        )

        mock_spawn_context = MagicMock()
        mock_spawn_context.session_id = "child"
        mock_spawn_context.agent_run_id = "run-1"
        mock_spawn_context.env_vars = {}

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=99,
            terminal_type="tmux",
            tmux_session_name="gobby-abc",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

        assert result.tmux_session_name == "gobby-abc"
        assert result.success is True
        assert result.pid == 99


class TestApplyExtraEnv:
    def test_reserved_env_overrides_are_ignored(self) -> None:
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="codex",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            extra_env={
                "GOBBY_SESSION_ID": "attacker-session",
                UV_CACHE_DIR: "/bad/cache",
                "CUSTOM_FLAG": "1",
            },
        )
        env = {
            "GOBBY_SESSION_ID": "gobby-sess-123",
            UV_CACHE_DIR: "/tmp/gobby/uv-cache/gobby-sess-123",
        }

        _apply_extra_env(env, request)

        assert env["GOBBY_SESSION_ID"] == "gobby-sess-123"
        assert env[UV_CACHE_DIR] == "/tmp/gobby/uv-cache/gobby-sess-123"
        assert env["CUSTOM_FLAG"] == "1"
