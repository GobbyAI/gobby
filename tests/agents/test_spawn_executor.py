"""Tests for SpawnExecutor unified spawn dispatch."""

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from gobby.agents.session import ChildSessionManager

from gobby.agents.constants import CARGO_HOME, UV_CACHE_DIR
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.spawn_cache_policy import PATH_ENV_VAR, hook_inbox_dir, managed_tool_bin_dir
from gobby.agents.spawn_executor import (
    _CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS,
    _CODEX_PREAPPROVED_GOBBY_TOOLS,
    SpawnRequest,
    SpawnResult,
    _apply_extra_env,
    _record_resume_launch_details,
    _sandbox_config_for_spawn,
    execute_spawn,
)
from gobby.mcp_proxy.server import GobbyDaemonTools

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_native_subagent_strip_warns(caplog: pytest.LogCaptureFixture) -> None:
    request = SpawnRequest(
        prompt="Review the plan",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        agent_name="plan-adversary",
    )

    with caplog.at_level(logging.WARNING, logger="gobby.agents.spawn_executor"):
        result = await execute_spawn(request)

    assert result.success is False
    warning = " ".join(caplog.messages)
    assert "plan-adversary" in warning
    assert "provider-native internal subagents" in warning
    assert "Task" in warning


def test_codex_preapproved_gobby_tools_exist_on_mcp_handler() -> None:
    """Keep spawned Codex approval overrides aligned with the Gobby MCP surface."""
    tool_names = {name for name in dir(GobbyDaemonTools) if not name.startswith("_")}
    for tool_name in _CODEX_PREAPPROVED_GOBBY_TOOLS:
        assert tool_name in tool_names


def test_record_resume_launch_details_uses_resolved_agent_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = object()
    session_manager = SimpleNamespace(_storage=SimpleNamespace(db=db))
    request = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="codex",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        agent_run_id="original-run",
        session_manager=session_manager,
        extra_env={UV_CACHE_DIR: "/request/uv", "REQUEST_ONLY": "request"},
        resume_metadata_json={
            "provider": "codex",
            "env": {CARGO_HOME: "/persisted/cargo", "PERSISTED": "old"},
        },
    )
    calls: list[tuple[object, str, dict[str, object]]] = []

    class FakeAgentRunManager:
        def __init__(self, database: object) -> None:
            self.database = database

        def update_resume_metadata(self, agent_run_id: str, metadata: dict[str, object]) -> None:
            calls.append((self.database, agent_run_id, metadata))

    monkeypatch.setattr("gobby.storage.agents.LocalAgentRunManager", FakeAgentRunManager)

    _record_resume_launch_details(
        request,
        agent_run_id="resolved-run",
        sandbox_args=["--sandbox"],
        env={UV_CACHE_DIR: "/launch/uv", "LAUNCH_ONLY": "yes"},
        mcp_path="/path/.mcp.json",
        strict_mcp=True,
    )

    assert calls == [
        (
            db,
            "resolved-run",
            {
                "provider": "codex",
                "sandbox_args": ["--sandbox"],
                "sandbox_env": {},
                "env": {
                    CARGO_HOME: "/persisted/cargo",
                    UV_CACHE_DIR: "/launch/uv",
                },
                "config_overrides": [],
                "mcp_path": "/path/.mcp.json",
                "strict_mcp": True,
            },
        )
    ]


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
        """Test that terminal mode dispatches to TmuxSpawner."""
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
    async def test_qwen_terminal_calls_prepare_terminal_spawn(self):
        """Qwen direct spawn passes GOBBY_SESSION_ID env vars to the terminal."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="qwen",
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
                return_value=(["qwen", "--approval-mode", "yolo", "-i", "prompt"], {}),
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
            env_vars={
                "GOBBY_SESSION_ID": "gobby-sess-123",
                "GOBBY_PROJECT_ID": "proj",
                "GOBBY_AGENT_RUN_ID": "run-abc123def456",
                "GOBBY_AGENT_API_TOKEN": "scoped-token",
            },
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
            assert "mcp_servers.gobby.tool_timeout_sec=360" in command
            assert 'mcp_servers.gobby.tools.list_mcp_servers.approval_mode="approve"' in command
            assert 'mcp_servers.gobby.tools.list_tools.approval_mode="approve"' in command
            assert 'mcp_servers.gobby.tools.get_tool_schema.approval_mode="approve"' in command
            assert 'mcp_servers.gobby.tools.call_tool.approval_mode="approve"' in command
            assert 'mcp_servers.gobby.env.GOBBY_SESSION_ID="gobby-sess-123"' in command
            assert 'mcp_servers.gobby.env.GOBBY_PROJECT_ID="proj"' in command
            assert 'mcp_servers.gobby.env.GOBBY_AGENT_RUN_ID="run-abc123def456"' in command
            # The capability is forwarded by name; its value never enters argv.
            assert 'mcp_servers.gobby.env_vars=["GOBBY_AGENT_API_TOKEN"]' in command
            assert not any("scoped-token" in argument for argument in command)
            # Deliberately withheld: schema leases must resolve to the child
            # session, never the parent (see _CODEX_GOBBY_MCP_IDENTITY_ENV_VARS).
            assert not any("GOBBY_PARENT_SESSION_ID" in argument for argument in command)
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
    async def test_codex_terminal_spawn_local_oss_model(self) -> None:
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
            agent_run_id="run-local123456",
            session_manager=mock_session_manager,
            model="ollama/qwen3-coder",
            is_local=True,
            codex_oss_provider="ollama",
        )
        spawn_context = MagicMock(
            session_id="gobby-sess-local",
            agent_run_id="run-local123456",
            env_vars={"GOBBY_SESSION_ID": "gobby-sess-local"},
        )
        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="tmux",
            tmux_session_name="agent-run-local123456",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=spawn_context,
            ) as mock_prepare,
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
            patch("gobby.agents.spawn_executor.pre_approve_directory"),
        ):
            result = await execute_spawn(request)

        mock_prepare.assert_called_once()
        assert mock_prepare.call_args.kwargs["is_local"] is True
        command = mock_spawner.spawn.call_args.kwargs["command"]
        assert command[:6] == [
            "codex",
            "--oss",
            "--local-provider",
            "ollama",
            "-m",
            "ollama/qwen3-coder",
        ]
        assert result.success is True

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
        assert call_order == ["prepare", "env", "command", "preapprove", "spawn"]

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
        mock_session_manager.update_sandbox_enabled.assert_called_once_with(
            "gobby-sess-123",
            True,
        )
        command = mock_spawner.spawn.call_args.kwargs["command"]
        assert "--ask-for-approval" in command
        assert command[command.index("--ask-for-approval") + 1] == "never"
        assert command[command.index("--disable") + 1] == "guardian_approval"
        assert "--sandbox" in command
        assert "workspace-write" in command
        assert "-c" in command
        assert "sandbox_workspace_write.network_access=true" in command
        assert "--add-dir" in command
        assert command[command.index("--add-dir") + 1] == str(
            Path("/tmp/gobby/uv-cache/gobby-sess-123").resolve()
        )
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
    async def test_unknown_terminal_spawn_is_rejected(self):
        """Unsupported providers must not fall through to Claude."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="unknown",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            # No session_manager provided
        )

        result = await execute_spawn(request)

        assert result.success is False
        assert "Unsupported spawn provider: unknown" in (result.error or "")

    @pytest.mark.asyncio
    async def test_qwen_terminal_spawn_failure_propagates_error(self):
        """Qwen spawn failure is properly propagated to SpawnResult."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="qwen",
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
                return_value=(["qwen", "--approval-mode", "yolo", "-i", "prompt"], {}),
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
        mock_session_manager.update_sandbox_enabled.assert_called_once_with(
            "gobby-sess-123",
            True,
        )
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
        """Test that sandbox_config is resolved and passed to TmuxSpawner."""
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
            patch("gobby.agents.spawn_executor.get_sandbox_resolver", return_value=mock_resolver),
            patch("gobby.agents.spawn_executor.pre_approve_directory"),
        ):
            result = await execute_spawn(request)

        # Verify sandbox was resolved and env vars passed to spawn
        mock_resolver.resolve.assert_called_once()
        mock_session_manager.update_sandbox_enabled.assert_called_once_with(
            "child-session-id",
            True,
        )
        resolved_config = mock_resolver.resolve.call_args.args[0]
        assert "/tmp/gobby/uv-cache/child-session-id" in resolved_config.extra_write_paths
        mock_spawner.spawn.assert_called_once()
        call_kwargs = mock_spawner.spawn.call_args.kwargs
        assert "env" in call_kwargs
        assert "SEATBELT_PROFILE" in call_kwargs["env"]
        assert call_kwargs["env"][UV_CACHE_DIR] == "/tmp/gobby/uv-cache/child-session-id"
        assert call_kwargs["env"][CARGO_HOME]
        cargo_home_parts = Path(call_kwargs["env"][CARGO_HOME]).parts
        assert cargo_home_parts[-3:-1] == ("gobby", "cargo-home")
        assert cargo_home_parts[-1].startswith("child-session-id-")
        assert call_kwargs["env"][CARGO_HOME] in resolved_config.extra_write_paths
        # Command should include sandbox args
        command = call_kwargs.get("command")
        assert "--dangerously-skip-permissions" in command
        assert "--settings" in command
        assert result.success is True

    def test_sandbox_config_for_spawn_adds_policy_write_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Sandboxed agents get writable caches and a hook inbox, not managed binaries."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        env_vars = {
            "GOBBY_SESSION_ID": "child/session:one",
            UV_CACHE_DIR: "/tmp/gobby/uv-cache/child-session-one",
        }
        config = SandboxConfig(enabled=True, extra_write_paths=["/already-allowed"])

        resolved = _sandbox_config_for_spawn(config, env_vars)

        assert resolved is not None
        assert resolved.enabled is True
        assert env_vars[CARGO_HOME]
        cargo_home_parts = Path(env_vars[CARGO_HOME]).parts
        assert cargo_home_parts[-3:-1] == ("gobby", "cargo-home")
        assert cargo_home_parts[-1].startswith("child-session-one-")
        assert "/already-allowed" in resolved.extra_write_paths
        assert "/tmp/gobby/uv-cache/child-session-one" in resolved.extra_write_paths
        assert env_vars[CARGO_HOME] in resolved.extra_write_paths
        assert hook_inbox_dir() in resolved.extra_write_paths
        assert managed_tool_bin_dir() not in resolved.extra_write_paths
        assert config.extra_write_paths == ["/already-allowed"]

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
        mock_session_manager.update_sandbox_enabled.assert_called_once_with(
            "child-session-id",
            False,
        )
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
        mock_session_manager.update_sandbox_enabled.assert_called_once_with(
            "child-session-id",
            False,
        )
        # Env should only have gobby session vars, no sandbox vars (sandbox disabled)
        assert "SEATBELT_PROFILE" not in call_kwargs.get("env", {})
        assert result.success is True

    @pytest.mark.asyncio
    async def test_qwen_terminal_spawn_with_sandbox_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Qwen terminal spawn applies sandbox config correctly."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        git_dir = tmp_path / "repo" / ".git"
        git_worktree_dir = git_dir / "worktrees" / "agent"
        extra_write_path = tmp_path / "extra" / "path"
        uv_cache_dir = tmp_path / "gobby" / "uv-cache" / "gobby-sess-123"
        monkeypatch.setattr(
            "gobby.agents.sandbox._git_metadata_write_paths",
            lambda _workspace: [str(git_dir), str(git_worktree_dir)],
        )
        sandbox_config = SandboxConfig(
            enabled=True,
            mode="permissive",
            extra_write_paths=[str(extra_write_path)],
        )
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Test with sandbox",
            cwd=str(workspace),
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
                env_vars={
                    "GOBBY_SESSION_ID": "gobby-sess-123",
                    UV_CACHE_DIR: str(uv_cache_dir),
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
        mock_session_manager.update_sandbox_enabled.assert_called_once_with(
            "gobby-sess-123",
            True,
        )
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
        include_dirs = [
            Path(command[index + 1])
            for index, arg in enumerate(command)
            if arg == "--include-directories"
        ]
        assert len(include_dirs) <= 5
        for expected_path in [extra_write_path, uv_cache_dir, git_dir, git_worktree_dir]:
            resolved_expected_path = expected_path.resolve(strict=False)
            assert any(
                resolved_expected_path == include_dir
                or resolved_expected_path.is_relative_to(include_dir)
                for include_dir in include_dirs
            )
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
                "gobby.agents.spawn_executor.get_sandbox_resolver", return_value=mock_resolver
            ) as mock_get_resolver,
            patch(
                "gobby.agents.spawn_executor.TmuxSpawner",
                return_value=mock_spawner,
            ),
        ):
            result = await execute_spawn(request)

        mock_get_resolver.assert_called_once_with("qwen")
        mock_resolver.resolve.assert_called_once()
        mock_session_manager.update_sandbox_enabled.assert_called_once_with(
            "gobby-sess-123",
            True,
        )
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
    async def test_claude_terminal_uses_workspace_mcp_config(self, tmp_path: Path) -> None:
        """Claude isolated spawns must not fall back to conflicting user MCP scope."""
        (tmp_path / ".mcp.json").write_text(
            '{"mcpServers":{"gobby":{"command":"uv","args":["run","gobby","mcp-server"]}}}'
        )
        request = SpawnRequest(
            prompt="Test",
            cwd=str(tmp_path),
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=MagicMock(),
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

        command = mock_spawner.spawn.call_args.kwargs["command"]
        mcp_config_path = str(tmp_path / ".mcp.json")
        assert result.success is True
        assert command[-1] == "Test"
        assert "--mcp-config" in command
        assert command[command.index("--mcp-config") + 1] == mcp_config_path
        assert "--strict-mcp-config" in command
        assert command.index("--strict-mcp-config") < command.index("Test")

    @pytest.mark.asyncio
    async def test_claude_terminal_disallows_native_delegation_tools(self) -> None:
        """Managed Claude agents must not escape into native Workflow or Task delegation."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=MagicMock(),
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

        command = mock_spawner.spawn.call_args.kwargs["command"]
        assert result.success is True
        assert "--disallowedTools" in command
        start = command.index("--disallowedTools") + 1
        stop = start + len(_CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS)
        assert command[start:stop] == _CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS
        assert command.index("--disallowedTools") < command.index("--dangerously-skip-permissions")
        assert command.index("--dangerously-skip-permissions") < command.index("Test")

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
    def test_path_extra_env_is_merged_with_managed_bin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="codex",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            extra_env={PATH_ENV_VAR: os.pathsep.join(("/work/.gobby/bin", "/usr/bin"))},
        )
        env = {PATH_ENV_VAR: "/bin"}

        _apply_extra_env(env, request)

        assert env[PATH_ENV_VAR].split(os.pathsep) == [
            "/work/.gobby/bin",
            managed_tool_bin_dir(),
            "/usr/bin",
            "/bin",
        ]

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
                CARGO_HOME: "/bad/cargo-home",
                "CUSTOM_FLAG": "1",
            },
        )
        env = {
            "GOBBY_SESSION_ID": "gobby-sess-123",
            UV_CACHE_DIR: "/tmp/gobby/uv-cache/gobby-sess-123",
            CARGO_HOME: "/tmp/gobby/cargo-home/gobby-sess-123",
        }

        _apply_extra_env(env, request)

        assert env["GOBBY_SESSION_ID"] == "gobby-sess-123"
        assert env[UV_CACHE_DIR] == "/tmp/gobby/uv-cache/gobby-sess-123"
        assert env[CARGO_HOME] == "/tmp/gobby/cargo-home/gobby-sess-123"
        assert env["CUSTOM_FLAG"] == "1"


def test_codex_mcp_overrides_point_subprocess_tmpdir_at_sandbox_scratchpad() -> None:
    """Codex scrubs env for stdio MCP servers, so TMPDIR must be an explicit override."""
    from gobby.agents.spawn_executor_support import _codex_mcp_config_overrides

    scratchpad = "/Users/dev/.gobby/run/sandbox/run-1/tmp"
    overrides = _codex_mcp_config_overrides("/repo", scratchpad)

    assert f'mcp_servers.gobby.env.TMPDIR="{scratchpad}"' in overrides


def test_codex_mcp_overrides_omit_tmpdir_without_sandbox() -> None:
    """An unsandboxed spawn has no per-run scratchpad to redirect into."""
    from gobby.agents.spawn_executor_support import _codex_mcp_config_overrides

    overrides = _codex_mcp_config_overrides("/repo", None)

    assert not any(entry.startswith("mcp_servers.gobby.env.TMPDIR") for entry in overrides)


def test_capability_token_never_in_argv_or_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capability value stays in process env; argv and metadata carry only its name."""
    import json

    from gobby.agents.spawn_executor_support import _codex_mcp_config_overrides

    token_value = "gobby-agent-v1.payload.signature"
    identity_env = {
        "GOBBY_SESSION_ID": "child-session-uuid",
        "GOBBY_PROJECT_ID": "project-uuid",
        "GOBBY_AGENT_RUN_ID": "run-uuid",
        "GOBBY_AGENT_API_TOKEN": token_value,
    }

    overrides = _codex_mcp_config_overrides("/repo", None, managed_identity_env=identity_env)

    assert 'mcp_servers.gobby.env_vars=["GOBBY_AGENT_API_TOKEN"]' in overrides
    assert 'mcp_servers.gobby.env.GOBBY_SESSION_ID="child-session-uuid"' in overrides
    assert 'mcp_servers.gobby.env.GOBBY_AGENT_RUN_ID="run-uuid"' in overrides
    assert not any(token_value in override for override in overrides)

    db = object()
    session_manager = SimpleNamespace(_storage=SimpleNamespace(db=db))
    request = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="codex",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        agent_run_id="run-uuid",
        session_manager=cast("ChildSessionManager", session_manager),
        resume_metadata_json={"provider": "codex"},
    )
    persisted: list[dict[str, object]] = []

    class FakeAgentRunManager:
        def __init__(self, database: object) -> None:
            self.database = database

        def update_resume_metadata(self, agent_run_id: str, metadata: dict[str, object]) -> None:
            persisted.append(metadata)

    monkeypatch.setattr("gobby.storage.agents.LocalAgentRunManager", FakeAgentRunManager)

    _record_resume_launch_details(
        request,
        agent_run_id="run-uuid",
        config_overrides=[
            *overrides,
            # A legacy-style literal token override must never be persisted.
            f"mcp_servers.gobby.env.GOBBY_AGENT_API_TOKEN={json.dumps(token_value)}",
        ],
    )

    (metadata,) = persisted
    persisted_overrides = metadata["config_overrides"]
    assert isinstance(persisted_overrides, list)
    assert 'mcp_servers.gobby.env_vars=["GOBBY_AGENT_API_TOKEN"]' in persisted_overrides
    assert token_value not in json.dumps(metadata)


@pytest.mark.asyncio
async def test_scrubbed_child_env_reaches_daemon_proxy_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The Codex scrub model alone carries the child identity to the proxy.

    Codex launches its stdio MCP subprocess with a scrubbed environment: the
    child sees only the literal ``mcp_servers.gobby.env.*`` overrides plus the
    variables forwarded by name through ``env_vars``. This drives that exact
    model end-to-end and asserts DaemonProxy emits the child's
    ``X-Gobby-Session-Id`` on both the schema fetch and the following tool
    call, so the ``get_tool_schema`` lease and the ``call_tool`` resolve to
    the same session.
    """
    import json

    from gobby.agents.constants import get_terminal_env_vars
    from gobby.agents.spawn_executor_support import _codex_mcp_config_overrides
    from gobby.mcp_proxy.stdio_proxy import DaemonProxy
    from gobby.utils.local_token import local_token_path

    child_session_id = "11111111-2222-3333-4444-555555555555"
    parent_session_id = "99999999-8888-7777-6666-000000000000"
    run_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    local_token_path().write_text("operator-token\n")
    parent_env = get_terminal_env_vars(
        session_id=child_session_id,
        parent_session_id=parent_session_id,
        agent_run_id=run_id,
        project_id="project-uuid",
        operator_token="operator-token",
    )

    overrides = _codex_mcp_config_overrides("/main/repo", managed_identity_env=parent_env)

    # Rebuild the child environment exactly as Codex does for stdio MCP
    # servers: literal env.* overrides, plus env_vars names copied from the
    # provider process environment. No other variable is inherited.
    child_env: dict[str, str] = {}
    literal_prefix = "mcp_servers.gobby.env."
    for override in overrides:
        key, _, raw_value = override.partition("=")
        if key.startswith(literal_prefix):
            child_env[key.removeprefix(literal_prefix)] = json.loads(raw_value)
        elif key == "mcp_servers.gobby.env_vars":
            for name in json.loads(raw_value):
                child_env[name] = parent_env[name]

    # Deliberately withheld: schema leases must resolve to the child session,
    # never the parent (see _CODEX_GOBBY_MCP_IDENTITY_ENV_VARS).
    assert "GOBBY_PARENT_SESSION_ID" not in child_env
    assert child_env["GOBBY_SESSION_ID"] == child_session_id
    capability = child_env["GOBBY_AGENT_API_TOKEN"]

    for variable in parent_env:
        if variable.startswith("GOBBY_"):
            monkeypatch.delenv(variable, raising=False)
    for variable, value in child_env.items():
        monkeypatch.setenv(variable, value)

    response = MagicMock(status_code=200)
    response.json.return_value = {"success": True}
    client = MagicMock()
    client.request = AsyncMock(return_value=response)
    deps = MagicMock()
    deps.read_project_id.side_effect = lambda: os.environ.get("GOBBY_PROJECT_ID")
    deps.http_client_factory.return_value = client
    proxy = DaemonProxy(60887, deps_factory=lambda: deps)

    await proxy.get_tool_schema("gobby-tasks", "list_tasks")
    await proxy.call_tool("gobby-tasks", "list_tasks", {}, preflight_enabled=False)

    assert client.request.await_count == 2
    for request_call in client.request.await_args_list:
        headers = request_call.kwargs["headers"]
        assert headers["X-Gobby-Session-Id"] == child_session_id
        assert headers["X-Gobby-Agent-Run-Id"] == run_id
        assert headers["Authorization"] == f"Bearer {capability}"
        assert all(parent_session_id not in value for value in headers.values())
