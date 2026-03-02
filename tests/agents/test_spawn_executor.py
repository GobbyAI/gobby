"""
Tests for SpawnExecutor unified spawn dispatch.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn_executor import (
    SpawnRequest,
    SpawnResult,
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
            mode="terminal",
            provider="claude",
            session_id="session-123",
            run_id="run-456",
            parent_session_id="parent-789",
            project_id="proj-abc",
        )

        assert request.prompt == "Test prompt"
        assert request.cwd == "/path/to/project"
        assert request.mode == "terminal"
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
            mode="terminal",
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
            mode="terminal",
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
            mode="terminal",
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
        assert result.master_fd is None
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
            mode="terminal",
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
        mock_spawn_context.env_vars = {"GOBBY_SESSION_ID": "child-session-id"}

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
    async def test_autonomous_mode_requires_session_manager(self):
        """Test that autonomous mode requires session_manager."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            mode="autonomous",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
        )

        result = await execute_spawn(request)

        assert result.success is False
        assert "session_manager is required" in result.error

    @pytest.mark.asyncio
    async def test_spawn_failure_propagates_error(self):
        """Test that spawn failure returns error in result."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            mode="terminal",
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
        mock_spawn_context.env_vars = {"GOBBY_SESSION_ID": "child-session-id"}

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
            mode="terminal",
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
            ) as mock_prepare,
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            await execute_spawn(request)

            # Workflow is passed to prepare_terminal_spawn, not directly to spawner
            mock_prepare.assert_called_once()
            call_kwargs = mock_prepare.call_args.kwargs
            assert call_kwargs.get("workflow_name") == "auto-task"

    @pytest.mark.asyncio
    async def test_gemini_terminal_calls_prepare_terminal_spawn(self):
        """Test that provider='gemini' with mode='terminal' uses direct spawn with env vars.

        Gemini now uses direct spawn with GOBBY_SESSION_ID env var passed to the terminal.
        Session linkage happens when Gemini's hook dispatcher sends the env vars to daemon.
        """
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            mode="terminal",
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
                env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
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
                return_value=["gemini", "--approval-mode", "yolo", "-i", "prompt"],
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
    async def test_codex_terminal_calls_preflight(self):
        """Test that provider='codex' with mode='terminal' calls prepare_codex_spawn_with_preflight."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            mode="terminal",
            provider="codex",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
        )

        mock_preflight = AsyncMock(
            return_value=MagicMock(
                session_id="gobby-sess-123",
                env_vars={"GOBBY_CODEX_EXTERNAL_ID": "codex-ext-789"},
            )
        )

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_codex_spawn_with_preflight",
                mock_preflight,
            ),
            patch(
                "gobby.agents.spawn_executor.build_codex_command_with_resume",
                return_value=["codex", "--resume"],
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

            mock_preflight.assert_called_once()
            assert result.success is True
            assert result.codex_session_id == "codex-ext-789"

    @pytest.mark.asyncio
    async def test_claude_terminal_requires_session_manager(self):
        """Test that Claude spawn requires session_manager."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            mode="terminal",
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
            mode="terminal",
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
            mode="terminal",
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
                env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
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
                return_value=["gemini", "--approval-mode", "yolo", "-i", "prompt"],
            ),
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

            assert result.success is False
            assert "Terminal not found" in (result.error or "")


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
            mode="terminal",
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
        ):
            result = await execute_spawn(request)

            # Verify sandbox was resolved and env vars passed to spawn
            mock_resolver.resolve.assert_called_once()
            mock_spawner.spawn.assert_called_once()
            call_kwargs = mock_spawner.spawn.call_args.kwargs
            assert "env" in call_kwargs
            assert "SEATBELT_PROFILE" in call_kwargs["env"]
            # Command should include sandbox args
            command = call_kwargs.get("command")
            assert "--settings" in command
            assert result.success is True

    @pytest.mark.asyncio
    async def test_terminal_spawn_without_sandbox_passes_none(self) -> None:
        """Test that spawn without sandbox doesn't add sandbox env vars."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test without sandbox",
            cwd="/path",
            mode="terminal",
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
    async def test_autonomous_spawn_without_session_manager_and_sandbox(self) -> None:
        """Test that autonomous mode without session_manager fails even with sandbox."""
        sandbox_config = SandboxConfig(enabled=True, mode="restrictive")
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            mode="autonomous",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            sandbox_config=sandbox_config,
        )

        result = await execute_spawn(request)
        assert result.success is False
        assert "session_manager is required" in result.error

    @pytest.mark.asyncio
    async def test_sandbox_disabled_explicitly_passed(self) -> None:
        """Test that explicitly disabled sandbox doesn't add sandbox env vars."""
        sandbox_config = SandboxConfig(enabled=False)
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            mode="terminal",
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
        sandbox_config = SandboxConfig(enabled=True, mode="permissive")
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test with sandbox",
            cwd="/path",
            mode="terminal",
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
                env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
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
                return_value=["gemini", "--approval-mode", "yolo", "-i", "prompt"],
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
            assert "-s" in command
            assert result.success is True
