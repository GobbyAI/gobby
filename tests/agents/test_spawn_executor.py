"""Tests for SpawnExecutor unified spawn dispatch."""

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypedDict, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

if TYPE_CHECKING:
    from gobby.agents.session import ChildSessionManager

from gobby.agents import spawn_executor_support
from gobby.agents.constants import CARGO_HOME, UV_CACHE_DIR
from gobby.agents.sandbox import ResolvedSandboxPaths, SandboxConfig
from gobby.agents.spawn import PreparedSpawn
from gobby.agents.spawn_cache_policy import PATH_ENV_VAR, hook_inbox_dir, managed_tool_bin_dir
from gobby.agents.spawn_executor import (
    _CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS,
    _CODEX_PREAPPROVED_GOBBY_TOOLS,
    SpawnRequest,
    SpawnResult,
    _apply_extra_env,
    _prepare_managed_code_index,
    _record_resume_launch_details,
    _sandbox_config_for_spawn,
    execute_spawn,
)
from gobby.agents.spawn_executor_support import (
    _deliver_codex_prompt,
    schedule_codex_prompt_delivery,
)
from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.storage.terminals import TerminalManager
from tests.agents.prepared_spawn import prepared_spawn
from gobby.terminals.runtime import Delivered
from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore

pytestmark = pytest.mark.unit


def _runtime_of(request: SpawnRequest) -> FakeRuntime:
    registry = request.terminal_runtime_registry
    assert registry is not None
    runtime = registry.resolve(request.terminal_backend)
    assert isinstance(runtime, FakeRuntime)
    return runtime


def _manager_of(request: SpawnRequest) -> MemoryTerminalStore:
    manager = request.terminal_manager
    assert isinstance(manager, MemoryTerminalStore)
    return manager


class _SpawnKwargs(TypedDict):
    command: list[str]
    cwd: str | None
    env: dict[str, str]
    auth_cli: str | None


def _spawn_kwargs(request: SpawnRequest) -> _SpawnKwargs:
    spawned = _runtime_of(request).last_request
    assert spawned is not None
    return {
        "command": spawned.command,
        "cwd": spawned.cwd,
        "env": dict(spawned.env or {}),
        "auth_cli": spawned.auth_cli,
    }


@pytest.fixture(autouse=True)
def mock_codex_prompt_delivery() -> Iterator[MagicMock]:
    """Keep the fire-and-forget Codex prompt delivery task out of spawn tests.

    Codex spawns schedule a real background coroutine against the spawner's
    tmux session manager; against MagicMock spawners that coroutine would
    outlive the test's event loop. Tests that assert delivery use this mock.
    """
    with patch("gobby.agents.spawn_executor.schedule_codex_prompt_delivery") as mock_delivery:
        yield mock_delivery


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_token_env", "expected_probe_token"),
    [
        # The run-scoped managed credential must win; isolation gcode
        # rejects the operator token for managed probes (#19709).
        ({"GOBBY_AGENT_API_TOKEN": "run-scoped-token"}, "run-scoped-token"),
        # Tokenless-dev fallback: no minted capability -> operator token.
        ({}, "probe-token"),
    ],
)
async def test_managed_code_index_preflight_uses_issued_credential(
    monkeypatch: pytest.MonkeyPatch,
    run_token_env: dict[str, str],
    expected_probe_token: str,
) -> None:
    credential = MagicMock()
    context = PreparedSpawn(
        session_id="child",
        agent_run_id="run",
        parent_session_id="parent",
        project_id="project",
        workflow_name=None,
        agent_depth=1,
        env_vars={
            "GOBBY_AGENT_RUN_ID": "run-id-env",
            "GOBBY_PROJECT_ID": "project-id-env",
            "GOBBY_SESSION_ID": "session-id-env",
            "GOBBY_WORKFLOW_NAME": "planner",
            **run_token_env,
        },
        managed_credential=credential,
    )
    request = SpawnRequest(
        prompt="Plan",
        cwd="/isolated",
        provider="codex",
        session_id="session",
        run_id="run",
        parent_session_id="parent",
        project_id="project",
        code_index_preflight_mode="required",
        code_index_api_token="probe-token",
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )

    async def preflight(
        cwd: str,
        *,
        credential: object,
        api_token: str | None,
        identity_env: dict[str, str] | None = None,
    ) -> SimpleNamespace:
        assert cwd == "/isolated"
        assert credential is context.managed_credential
        assert api_token == expected_probe_token
        assert identity_env == {
            "GOBBY_AGENT_RUN_ID": "run-id-env",
            "GOBBY_PROJECT_ID": "project-id-env",
            "GOBBY_SESSION_ID": "session-id-env",
        }
        return SimpleNamespace(env={"PATH": "/scoped/bin"})

    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers.ensure_isolation_code_index", preflight
    )

    error = await _prepare_managed_code_index(request, context)

    assert error is None
    assert context.env_vars["PATH"] == "/scoped/bin"


@pytest.mark.asyncio
async def test_required_managed_code_index_preflight_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = PreparedSpawn(
        session_id="child",
        agent_run_id="run",
        parent_session_id="parent",
        project_id="project",
        workflow_name=None,
        agent_depth=1,
        env_vars={},
        managed_credential=MagicMock(),
    )
    request = SpawnRequest(
        prompt="Plan",
        cwd="/isolated",
        provider="codex",
        session_id="session",
        run_id="run",
        parent_session_id="parent",
        project_id="project",
        code_index_preflight_mode="required",
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )

    async def fail_preflight(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("scoped bootstrap missing")

    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers.ensure_isolation_code_index",
        fail_preflight,
    )

    error = await _prepare_managed_code_index(request, context)

    assert error is not None
    assert error.success is False
    assert error.error == "planner_code_index_unavailable:scoped bootstrap missing"


@pytest.mark.asyncio
async def test_best_effort_preflight_records_warning_without_operator_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = PreparedSpawn(
        session_id="child",
        agent_run_id="run",
        parent_session_id="parent",
        project_id="project",
        workflow_name=None,
        agent_depth=1,
        env_vars={},
        managed_credential=MagicMock(),
    )
    session_manager = MagicMock()
    request = SpawnRequest(
        prompt="Implement",
        cwd="/isolated",
        provider="codex",
        session_id="session",
        run_id="run",
        parent_session_id="parent",
        project_id="project",
        session_manager=session_manager,
        initial_variables={"additional_skills": ["code-index", "python"]},
        code_index_preflight_mode="best_effort",
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )

    async def fail_preflight(*_args: object, **kwargs: object) -> None:
        assert "database_url" not in kwargs
        assert "daemon_config" not in kwargs
        raise RuntimeError("scoped bootstrap missing")

    variable_manager = MagicMock()
    monkeypatch.setattr(
        "gobby.agents.spawn_executor_providers.ensure_isolation_code_index",
        fail_preflight,
    )
    with patch(
        "gobby.workflows.state_manager.SessionVariableManager",
        return_value=variable_manager,
    ):
        error = await _prepare_managed_code_index(request, context)

    assert error is None
    assert request.code_index_preflight_warning == {
        "preflight": "code_index",
        "cwd": "/isolated",
        "message": "scoped bootstrap missing",
    }
    assert "Code-index preflight failed: scoped bootstrap missing" in request.prompt
    assert request.initial_variables is not None
    assert request.initial_variables["additional_skills"] == ["python"]
    variable_manager.merge_variables.assert_called_once_with(
        "child",
        request.initial_variables,
    )


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
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
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
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            machine_id="21000000-0000-4000-8000-000000000002",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
        ):
            request.prepared_spawn = mock_spawn_context
            result = await execute_spawn(request)

            assert _runtime_of(request).last_request is not None
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
            machine_id="21000000-0000-4000-8000-000000000002",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
        ):
            request.prepared_spawn = mock_spawn_context
            _runtime_of(request).typed_fail = True
            _runtime_of(request).spawn_error = "Terminal not found"
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
            machine_id="21000000-0000-4000-8000-000000000002",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
        ):
            request.prepared_spawn = mock_spawn_context
            await execute_spawn(request)

            mock_prepare.assert_not_called()
            assert request.workflow == "auto-task"
            assert request.prepared_spawn is not None

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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
                "gobby.agents.spawn_executor_providers.build_cli_command",
                return_value=(["qwen", "--approval-mode", "yolo", "-i", "prompt"], {}),
            ),
        ):
            request.prepared_spawn = mock_prepare.return_value
            result = await execute_spawn(request)

            mock_prepare.assert_not_called()
            assert _runtime_of(request).last_request is not None
            # Env vars ARE passed to spawn now (for hook dispatcher to read)
            call_kwargs = _spawn_kwargs(request)
            assert call_kwargs.get("env") is not None
            assert "GOBBY_SESSION_ID" in call_kwargs["env"]
            assert result.success is True
            assert result.child_session_id == "gobby-sess-123"
            assert result.pid == 12345

    async def test_qwen_spawn_preparation_keeps_event_loop_responsive(self) -> None:
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="qwen",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=MagicMock(),
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
        )
        request.prepared_spawn = prepared_spawn(
            session_id="gobby-sess-123",
            agent_run_id="run-abc123",
            env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
        )
        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(success=True, pid=12345)
        with (
            patch(
                "gobby.agents.spawn_executor_providers.build_cli_command",
                return_value=(["qwen"], {}),
            ),
        ):
            result = await execute_spawn(request)

        assert result.success is True
        assert result.pid == 12345
        assert _runtime_of(request).create_calls == 1

    @pytest.mark.asyncio
    async def test_codex_terminal_direct_spawn(self, mock_codex_prompt_delivery):
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
        request.prepared_spawn = spawn_context
        mock_prepare = MagicMock(
            side_effect=lambda **_kwargs: call_order.append("prepare") or spawn_context
        )

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="tmux",
            terminal_id="agent-run-abc123def456",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                mock_prepare,
            ),
            patch("gobby.agents.spawn_executor_providers.pre_approve_directory") as mock_preapprove,
        ):
            mock_preapprove.side_effect = lambda *_args, **_kwargs: call_order.append("preapprove")
            result = await execute_spawn(request)

            # prepare_terminal_spawn must be called with source='codex' and the
            # caller's agent_run_id threaded through unchanged.
            mock_preapprove.assert_called_once_with("codex", "/path")
            mock_prepare.assert_not_called()
            assert call_order == ["preapprove"]
            assert request.provider == "codex"
            assert request.agent_run_id == "run-abc123def456"

            # Command starts with `codex` and never invokes `resume`.
            spawn_kwargs = _spawn_kwargs(request)
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

            # The prompt never rides in argv: a CLI-argument prompt starts the
            # first turn at launch and cancels Codex's in-flight MCP client
            # startup. It is typed into the composer post-launch instead.
            assert request.prompt not in command
            mock_codex_prompt_delivery.assert_called_once()
            _coordinator, terminal, prompt, run_id = mock_codex_prompt_delivery.call_args.args
            assert terminal.id == result.terminal_id
            assert prompt == "Test"
            assert run_id == "run-abc123def456"

            # SpawnResult.run_id is the caller-minted id (no fabricated
            # `codex-xxxxxxxx` substitute).
            assert result.success is True
            assert result.run_id == "run-abc123def456"
            assert result.child_session_id == "gobby-sess-123"
            assert result.codex_session_id is None  # late-linked via SessionStart hook

    @pytest.mark.asyncio
    async def test_codex_persona_precedes_task_prompt(self, mock_codex_prompt_delivery):
        """The persona preamble rides ahead of the composer prompt and the
        first-turn injection is suppressed (#20451)."""
        mock_session_manager = MagicMock()
        request = SpawnRequest(
            prompt="Review the plan",
            cwd="/path",
            provider="codex",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            project_path="/main/repo",
            agent_run_id="run-abc123def456",
            agent_name="qa-reviewer",
            session_manager=mock_session_manager,
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
        )
        spawn_context = MagicMock(
            session_id="gobby-sess-123",
            agent_run_id="run-abc123def456",
            env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
        )
        request.prepared_spawn = spawn_context

        mock_spawner = MagicMock()
        mock_spawner.spawn.return_value = MagicMock(
            success=True,
            pid=12345,
            terminal_type="tmux",
            terminal_id="agent-run-abc123def456",
        )
        agent_body = MagicMock()
        agent_body.build_prompt_preamble.return_value = "## Persona\nYou are the QA reviewer."

        with (
            patch("gobby.agents.spawn_executor_providers.pre_approve_directory"),
            patch(
                "gobby.workflows.agent_resolver.resolve_agent",
                return_value=agent_body,
            ) as mock_resolve,
            patch("gobby.workflows.state_manager.SessionVariableManager") as mock_sv_mgr,
        ):
            result = await execute_spawn(request)

        assert result.success is True
        mock_resolve.assert_called_once()
        assert mock_resolve.call_args.args[0] == "qa-reviewer"
        delivered_prompt = mock_codex_prompt_delivery.call_args.args[2]
        assert delivered_prompt.startswith("## Persona\nYou are the QA reviewer.")
        assert delivered_prompt.endswith("Review the plan")
        mock_sv_mgr.return_value.merge_variables.assert_called_once_with(
            "gobby-sess-123",
            {"_agent_context_injected": True},
        )

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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            terminal_id="agent-run-local123456",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=spawn_context,
            ) as mock_prepare,
            patch("gobby.agents.spawn_executor_providers.pre_approve_directory"),
        ):
            result = await execute_spawn(request)

        mock_prepare.assert_not_called()
        assert request.is_local is True
        command = _spawn_kwargs(request)["command"]
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
        )
        call_order: list[str] = []
        spawn_context = MagicMock(
            session_id="gobby-sess-123",
            agent_run_id="run-abc123def456",
            env_vars={"GOBBY_SESSION_ID": "gobby-sess-123"},
        )
        request.prepared_spawn = spawn_context
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
                "gobby.agents.spawn_executor_providers.build_cli_command",
                side_effect=lambda **_kwargs: call_order.append("command")
                or (["codex", "Test"], {}),
            ),
            patch(
                "gobby.agents.spawn_executor_providers._apply_extra_env",
                fake_apply_extra_env,
            ),
            patch(
                "gobby.agents.spawn_executor_providers.pre_approve_directory",
                side_effect=lambda *_args, **_kwargs: call_order.append("preapprove"),
            ),
        ):
            result = await execute_spawn(request)

        assert result.success is True
        assert call_order == ["env", "command", "preapprove"]

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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            terminal_id="agent-run-xyz",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                mock_prepare,
            ),
            patch("gobby.agents.spawn_executor_providers.pre_approve_directory") as mock_preapprove,
        ):
            request.prepared_spawn = mock_prepare.return_value
            result = await execute_spawn(request)

        mock_preapprove.assert_not_called()
        assert _runtime_of(request).last_request is None
        assert result.success is False
        assert "codex cannot prove the sensitive-root contract" in (result.error or "")

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
            # No session_manager provided,
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            # No session_manager provided,
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
                "gobby.agents.spawn_executor_providers.build_cli_command",
                return_value=(["qwen", "--approval-mode", "yolo", "-i", "prompt"], {}),
            ),
        ):
            request.prepared_spawn = mock_prepare.return_value
            _runtime_of(request).typed_fail = True
            _runtime_of(request).spawn_error = "Terminal not found"
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            terminal_id="agent-run-grok123",
        )

        with (
            patch("gobby.agents.spawn_executor.prepare_terminal_spawn", mock_prepare),
            patch("gobby.agents.spawn_executor_providers.pre_approve_directory") as mock_preapprove,
        ):
            request.prepared_spawn = mock_prepare.return_value
            result = await execute_spawn(request)

        mock_prepare.assert_not_called()
        assert request.provider == "grok"
        mock_preapprove.assert_called_once_with("grok", "/path")

        spawn_kwargs = _spawn_kwargs(request)
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            patch("gobby.agents.spawn_executor_providers.pre_approve_directory"),
        ):
            request.prepared_spawn = mock_prepare.return_value
            result = await execute_spawn(request)

        assert _runtime_of(request).last_request is None
        assert result.success is False
        assert "grok cannot prove the sensitive-root contract" in (result.error or "")

    @pytest.mark.asyncio
    async def test_agy_spawn_rejects_unavailable_provider(self) -> None:
        """AGY is visible but explicitly unavailable for agent spawning."""
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="agy",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            machine_id="21000000-0000-4000-8000-000000000002",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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

        def resolve_with_sensitive_denies(
            _config: SandboxConfig,
            paths: ResolvedSandboxPaths,
        ) -> tuple[list[str], dict[str, str]]:
            settings = {
                "sandbox": {
                    "enabled": True,
                    "allowUnsandboxedCommands": False,
                    "filesystem": {
                        "denyRead": paths.deny_read_paths,
                        "denyWrite": paths.deny_write_paths,
                    },
                }
            }
            return ["--settings", json.dumps(settings)], {"SEATBELT_PROFILE": "verified"}

        mock_resolver.resolve.side_effect = resolve_with_sensitive_denies

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ),
            patch(
                "gobby.agents.spawn_executor_providers.get_sandbox_resolver",
                return_value=mock_resolver,
            ),
            patch("gobby.agents.spawn_executor_providers.pre_approve_directory"),
        ):
            request.prepared_spawn = mock_spawn_context
            result = await execute_spawn(request)

        # Verify sandbox was resolved and env vars passed to spawn
        mock_resolver.resolve.assert_called_once()
        mock_session_manager.update_sandbox_enabled.assert_called_once_with(
            "child-session-id",
            True,
        )
        resolved_config = mock_resolver.resolve.call_args.args[0]
        assert "/tmp/gobby/uv-cache/child-session-id" not in resolved_config.extra_write_paths
        assert {Path(path).name for path in resolved_config.extra_write_paths} == {
            "tmp",
            "hooks",
            "logs",
            "cache",
        }
        assert _runtime_of(request).last_request is not None
        call_kwargs = _spawn_kwargs(request)
        assert "env" in call_kwargs
        assert "SEATBELT_PROFILE" in call_kwargs["env"]
        assert call_kwargs["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert Path(call_kwargs["env"][UV_CACHE_DIR]).is_relative_to(
            Path(
                next(
                    path for path in resolved_config.extra_write_paths if Path(path).name == "cache"
                )
            )
        )
        assert call_kwargs["env"][CARGO_HOME]
        assert Path(call_kwargs["env"][CARGO_HOME]).is_relative_to(
            Path(
                next(
                    path for path in resolved_config.extra_write_paths if Path(path).name == "cache"
                )
            )
        )
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
            machine_id="21000000-0000-4000-8000-000000000002",
            # No sandbox_config specified,
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
        ):
            request.prepared_spawn = mock_spawn_context
            result = await execute_spawn(request)

        assert _runtime_of(request).last_request is not None
        call_kwargs = _spawn_kwargs(request)
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
            machine_id="21000000-0000-4000-8000-000000000002",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
        ):
            request.prepared_spawn = mock_spawn_context
            result = await execute_spawn(request)

        assert _runtime_of(request).last_request is not None
        call_kwargs = _spawn_kwargs(request)
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
        ):
            request.prepared_spawn = mock_prepare.return_value
            result = await execute_spawn(request)

        assert _runtime_of(request).last_request is None
        assert result.success is False
        assert "qwen cannot prove the sensitive-root contract" in (result.error or "")

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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
                "gobby.agents.spawn_executor_providers.get_sandbox_resolver",
                return_value=mock_resolver,
            ) as mock_get_resolver,
        ):
            request.prepared_spawn = mock_prepare.return_value
            result = await execute_spawn(request)

        mock_get_resolver.assert_called_once_with("qwen")
        mock_resolver.resolve.assert_not_called()
        assert _runtime_of(request).last_request is None
        assert result.success is False
        assert "qwen cannot prove the sensitive-root contract" in (result.error or "")


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
            # No session_manager,
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            patch("gobby.agents.spawn_executor_providers.pre_approve_directory") as mock_preapprove,
        ):
            request.prepared_spawn = mock_prepare.return_value
            _runtime_of(request).typed_fail = True
            _runtime_of(request).spawn_error = "tmux failed"
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
            machine_id="21000000-0000-4000-8000-00000000000e",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            terminal_id="gobby-test",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ),
        ):
            request.prepared_spawn = mock_spawn_context
            result = await execute_spawn(request)

        assert result.success is True
        call_kwargs = _spawn_kwargs(request)
        assert call_kwargs["env"]["GOBBY_MACHINE_ID"] == "21000000-0000-4000-8000-00000000000e"

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
            machine_id="21000000-0000-4000-8000-000000000022",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
        ):
            request.prepared_spawn = mock_spawn_context
            result = await execute_spawn(request)

        command = _spawn_kwargs(request)["command"]
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
            machine_id="21000000-0000-4000-8000-000000000022",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
        ):
            request.prepared_spawn = mock_spawn_context
            result = await execute_spawn(request)

        command = _spawn_kwargs(request)["command"]
        assert result.success is True
        assert "--disallowedTools" in command
        start = command.index("--disallowedTools") + 1
        stop = start + len(_CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS)
        assert command[start:stop] == _CLAUDE_MANAGED_AGENT_DISALLOWED_TOOLS
        assert command.index("--disallowedTools") < command.index("--dangerously-skip-permissions")
        assert command.index("--dangerously-skip-permissions") < command.index("Test")

    @pytest.mark.asyncio
    async def test_claude_terminal_terminal_id_in_result(self) -> None:
        """Test that terminal_id is propagated to SpawnResult."""
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
            machine_id="21000000-0000-4000-8000-000000000022",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            terminal_id="gobby-abc",
        )

        with (
            patch(
                "gobby.agents.spawn_executor.prepare_terminal_spawn",
                return_value=mock_spawn_context,
            ),
        ):
            request.prepared_spawn = mock_spawn_context
            result = await execute_spawn(request)

        assert result.terminal_id is not None
        row = _manager_of(request).get(result.terminal_id)
        assert row is not None
        assert row.spawn_key == f"gobby-{result.terminal_id}"
        assert row.session_name == f"gobby-{result.terminal_id}"
        assert result.success is True
        assert result.pid == 12345


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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
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


def test_codex_mcp_overrides_forward_spawn_cache_redirects() -> None:
    """Codex's env scrub drops the per-run toolchain caches, so the `uv run`
    bootstrap needs UV_CACHE_DIR as a literal override — the sandbox policy
    only grants writes to the redirected roots, not ~/.cache/uv."""
    from gobby.agents.spawn_executor_support import _codex_mcp_config_overrides

    managed_env = {
        "GOBBY_SESSION_ID": "child-session-uuid",
        UV_CACHE_DIR: "/tmp/gobby/uv-cache/child-session-uuid",
        CARGO_HOME: "/tmp/gobby/cargo-home/child-session-uuid",
    }

    overrides = _codex_mcp_config_overrides("/repo", None, managed_identity_env=managed_env)

    assert (
        'mcp_servers.gobby.env.UV_CACHE_DIR="/tmp/gobby/uv-cache/child-session-uuid"' in overrides
    )
    assert (
        'mcp_servers.gobby.env.CARGO_HOME="/tmp/gobby/cargo-home/child-session-uuid"' in overrides
    )


def test_codex_mcp_overrides_omit_absent_spawn_cache_redirects() -> None:
    """No cache redirect in the managed env means no literal override."""
    from gobby.agents.spawn_executor_support import _codex_mcp_config_overrides

    overrides = _codex_mcp_config_overrides(
        "/repo", None, managed_identity_env={"GOBBY_SESSION_ID": "child-session-uuid"}
    )

    assert not any("UV_CACHE_DIR" in entry for entry in overrides)
    assert not any("CARGO_HOME" in entry for entry in overrides)


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
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
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


class TestCodexPromptDelivery:
    """The spawn prompt is typed into the Codex composer, never passed in argv."""

    @pytest.mark.asyncio
    async def test_delivers_prompt_once_composer_renders(self) -> None:
        from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
        from tests.terminals.fakes import make_memory_terminal

        terminal = make_memory_terminal()
        store = MemoryTerminalStore(terminal)
        runtime = FakeRuntime()
        runtime.snapshot_text = "› Ask Codex anything"
        coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime)

        with (
            patch.object(spawn_executor_support, "_CODEX_COMPOSER_POLL_SECONDS", 0.0),
            patch.object(spawn_executor_support, "_CODEX_COMPOSER_SETTLE_SECONDS", 0.0),
            patch.object(spawn_executor_support, "_CODEX_PROMPT_SUBMIT_RETRY_DELAY_SECONDS", 0.0),
        ):
            await _deliver_codex_prompt(coordinator, terminal, "Do the task", "run-1")

        assert runtime.write_log == [("text", "Do the task"), ("key", "enter")]

    @pytest.mark.asyncio
    async def test_never_types_into_a_pane_without_a_composer(self) -> None:
        """A dead CLI can leave a shell pane; the watchdog owns that failure."""
        from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
        from tests.terminals.fakes import make_memory_terminal

        terminal = make_memory_terminal()
        store = MemoryTerminalStore(terminal)
        runtime = FakeRuntime()
        runtime.snapshot_text = "zsh: command not found: codex"
        coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime)

        with patch.object(spawn_executor_support, "_CODEX_COMPOSER_READY_TIMEOUT_SECONDS", 0.0):
            await _deliver_codex_prompt(coordinator, terminal, "Do the task", "run-1")

        assert runtime.write_log == []

    @pytest.mark.asyncio
    async def test_failed_paste_skips_follow_up_enter(self) -> None:
        from gobby.terminals.runtime import TerminalWriteError
        from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
        from tests.terminals.fakes import make_memory_terminal

        terminal = make_memory_terminal()
        store = MemoryTerminalStore(terminal)
        runtime = FakeRuntime()
        runtime.snapshot_text = "› "
        runtime.raise_on_write = TerminalWriteError(stage="none")
        coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime)

        with (
            patch.object(spawn_executor_support, "_CODEX_COMPOSER_SETTLE_SECONDS", 0.0),
            patch.object(spawn_executor_support, "_CODEX_PROMPT_SUBMIT_RETRY_DELAY_SECONDS", 0.0),
        ):
            await _deliver_codex_prompt(coordinator, terminal, "Do the task", "run-1")

        assert runtime.write_log == []

    @pytest.mark.asyncio
    async def test_schedule_skips_empty_prompt(self) -> None:
        assert schedule_codex_prompt_delivery(object(), object(), "", "run-1") is False

        assert not spawn_executor_support._CODEX_PROMPT_DELIVERY_TASKS

    @pytest.mark.asyncio
    async def test_schedule_tracks_and_releases_the_delivery_task(self) -> None:
        coordinator = object()
        terminal = object()

        with patch.object(
            spawn_executor_support, "_deliver_codex_prompt", new=AsyncMock()
        ) as deliver:
            assert schedule_codex_prompt_delivery(coordinator, terminal, "Go", "run-1") is True
            pending = list(spawn_executor_support._CODEX_PROMPT_DELIVERY_TASKS)
            assert pending
            await asyncio.gather(*pending)

        deliver.assert_awaited_once_with(coordinator, terminal, "Go", "run-1")
        assert not spawn_executor_support._CODEX_PROMPT_DELIVERY_TASKS

    @pytest.mark.asyncio
    async def test_codex_prompt_aborts_on_indeterminate_without_enter(self) -> None:
        from gobby.terminals.runtime import IndeterminateWrite
        from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
        from tests.terminals.fakes import make_memory_terminal

        terminal = make_memory_terminal()
        store = MemoryTerminalStore(terminal)
        runtime = FakeRuntime()
        runtime.outcomes = [IndeterminateWrite(detail="lost"), Delivered()]
        coordinator = WriteCoordinator(cast(UnresolvedWriteStore, store), runtime)
        runtime.snapshot_text = "› Ask Codex anything"

        with (
            patch.object(spawn_executor_support, "_CODEX_COMPOSER_POLL_SECONDS", 0.0),
            patch.object(spawn_executor_support, "_CODEX_COMPOSER_SETTLE_SECONDS", 0.0),
            patch.object(spawn_executor_support, "_CODEX_PROMPT_SUBMIT_RETRY_DELAY_SECONDS", 0.0),
        ):
            await _deliver_codex_prompt(
                coordinator,
                terminal,
                "Do the task",
                "run-1",
            )

        kinds = [kind for kind, _payload in runtime.write_log]
        assert "enter" not in kinds
        assert kinds == ["text"]


@pytest.mark.asyncio
async def test_one_terminal_row_per_attempt_all_outcomes() -> None:
    mock_session_manager = MagicMock()
    mock_session_manager._storage.db = MagicMock()

    async def _run(*, fail: bool = False, typed: bool = False) -> tuple[SpawnRequest, SpawnResult]:
        request = SpawnRequest(
            prompt="Test",
            cwd="/path",
            provider="claude",
            session_id="sess",
            run_id="run",
            parent_session_id="parent",
            project_id="proj",
            session_manager=mock_session_manager,
            machine_id="21000000-0000-4000-8000-000000000002",
            prepared_spawn=prepared_spawn(),
            terminal_backend="tmux",
        )
        runtime = _runtime_of(request)
        runtime.fail_spawn = fail
        runtime.typed_fail = typed
        result = await execute_spawn(request)
        return request, result

    request, result = await _run()
    manager = _manager_of(request)
    assert result.success is True
    assert len(manager.rows) == 1
    assert next(iter(manager.rows.values())).state == "live"

    request, result = await _run(typed=True)
    manager = _manager_of(request)
    assert result.success is False
    assert len(manager.rows) == 1
    assert next(iter(manager.rows.values())).state == "exited"

    request, result = await _run(fail=True)
    manager = _manager_of(request)
    assert result.success is False
    assert len(manager.rows) == 1
    assert next(iter(manager.rows.values())).state == "pending"


@pytest.mark.asyncio
async def test_in_doubt_pending_is_not_reaped_or_exited() -> None:
    from gobby.agents.spawn_executor import reap_stale_pending_terminals

    request = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        machine_id="21000000-0000-4000-8000-000000000002",
        timeout_seconds=0.01,
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )
    runtime = _runtime_of(request)
    runtime.delay = 1.0
    result = await execute_spawn(request)
    assert result.success is False
    assert "timed out" in (result.error or "")
    manager = _manager_of(request)
    row = next(iter(manager.rows.values()))
    assert row.state == "pending"
    assert runtime.killed  # spawn_key resolution killed the late effect
    reaped = await reap_stale_pending_terminals(
        cast(TerminalManager, manager), runtime, in_doubt_seconds=150.0
    )
    assert reaped == []
    pending = manager.get(row.id)
    assert pending is not None
    assert pending.state == "pending"

    retry = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        machine_id="21000000-0000-4000-8000-000000000002",
        retry_terminal_id=row.id,
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
        terminal_manager=cast(TerminalManager, manager),
        terminal_runtime_registry=request.terminal_runtime_registry,
    )
    runtime.delay = 0.0
    retried = await execute_spawn(retry)
    assert retried.success is True
    assert retried.terminal_id == row.id
    assert len(manager.rows) == 1


@pytest.mark.asyncio
async def test_srt_wrap_single_chokepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    wraps: list[list[str]] = []
    from gobby.agents.srt_runtime import SandboxLaunch

    real = SandboxLaunch.wrap

    def counting_wrap(self: SandboxLaunch, command: list[str]) -> list[str]:
        wraps.append(list(command))
        return real(self, command)

    monkeypatch.setattr(SandboxLaunch, "wrap", counting_wrap)
    request = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        machine_id="21000000-0000-4000-8000-000000000002",
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )
    result = await execute_spawn(request)
    assert result.success is True
    assert len(wraps) == 1


@pytest.mark.asyncio
async def test_lost_cas_converges_when_reconciler_already_promoted() -> None:
    request = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        machine_id="21000000-0000-4000-8000-000000000002",
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )
    manager = _manager_of(request)
    runtime = _runtime_of(request)
    original_promote = manager.promote_to_live

    def already_live(terminal_id: str, **kwargs: object) -> object:
        current = manager.get(terminal_id)
        assert current is not None
        current.state = "live"
        locator = kwargs["locator"]
        assert isinstance(locator, dict)
        current.locator = dict(locator)
        current.locator_key = str(kwargs["locator_key"])
        session_name = kwargs.get("session_name")
        current.session_name = session_name if isinstance(session_name, str) else None
        return None

    manager.promote_to_live = already_live  # type: ignore[method-assign, assignment]
    result = await execute_spawn(request)
    assert result.success is True
    assert not runtime.killed
    del original_promote


@pytest.mark.asyncio
async def test_cancel_windows_pre_and_post_dispatch() -> None:
    cancel = asyncio.Event()
    cancel.set()
    request = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        machine_id="21000000-0000-4000-8000-000000000002",
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
        cancel_event=cancel,
    )
    result = await execute_spawn(request)
    assert result.success is False
    assert result.error == "cancelled"
    assert _runtime_of(request).create_calls == 0

    hold = asyncio.Event()
    request2 = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        machine_id="21000000-0000-4000-8000-000000000002",
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )
    runtime = _runtime_of(request2)
    runtime.spawn_hold = hold
    task = asyncio.create_task(execute_spawn(request2))
    for _ in range(50):
        if runtime.create_calls:
            break
        await asyncio.sleep(0)
    task.cancel()
    hold.set()
    result2 = await task
    assert result2.success is False
    manager = _manager_of(request2)
    row = next(iter(manager.rows.values()))
    assert row.state == "pending"


@pytest.mark.asyncio
async def test_retry_generation_fences_the_reaper() -> None:
    request = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        machine_id="21000000-0000-4000-8000-000000000002",
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )
    runtime = _runtime_of(request)
    runtime.typed_fail = True
    await execute_spawn(request)
    manager = _manager_of(request)
    row = next(iter(manager.rows.values()))
    row.state = "pending"
    observed_gen = row.attempt_generation
    observed_started = row.attempt_started_at
    retry = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        machine_id="21000000-0000-4000-8000-000000000002",
        retry_terminal_id=row.id,
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
        terminal_manager=cast(TerminalManager, manager),
        terminal_runtime_registry=request.terminal_runtime_registry,
    )
    runtime.typed_fail = False
    retried = await execute_spawn(retry)
    assert retried.success is True
    lost = manager.fail_pending_attempt(
        row.id,
        attempt_generation=observed_gen,
        attempt_started_at=observed_started,
    )
    assert lost is None
    live = manager.get(row.id)
    assert live is not None
    assert live.state == "live"

    stale = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        machine_id="21000000-0000-4000-8000-000000000002",
        retry_terminal_id=row.id,
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
        terminal_manager=cast(TerminalManager, manager),
        terminal_runtime_registry=request.terminal_runtime_registry,
    )
    failed = await execute_spawn(stale)
    assert failed.success is False
    assert failed.error == "retry_terminal_not_pending"


@pytest.mark.asyncio
async def test_attempt_started_at_survives_unrelated_updates_and_restart() -> None:
    from datetime import UTC, datetime, timedelta

    request = SpawnRequest(
        prompt="Test",
        cwd="/path",
        provider="claude",
        session_id="sess",
        run_id="run",
        parent_session_id="parent",
        project_id="proj",
        session_manager=MagicMock(),
        machine_id="21000000-0000-4000-8000-000000000002",
        timeout_seconds=0.01,
        prepared_spawn=prepared_spawn(),
        terminal_backend="tmux",
    )
    runtime = _runtime_of(request)
    runtime.delay = 1.0
    await execute_spawn(request)
    manager = _manager_of(request)
    row = next(iter(manager.rows.values()))
    original_started = row.attempt_started_at
    original_gen = row.attempt_generation
    row.updated_at = datetime.now(UTC) + timedelta(hours=2)
    from gobby.agents.spawn_executor import reap_stale_pending_terminals

    reaped = await reap_stale_pending_terminals(
        cast(TerminalManager, manager), runtime, in_doubt_seconds=150.0
    )
    assert reaped == []
    assert row.attempt_started_at == original_started
    assert row.attempt_generation == original_gen
    assert row.state == "pending"
