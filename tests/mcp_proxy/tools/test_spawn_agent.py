"""
Tests for spawn_agent unified MCP tool.

Tests the spawn_agent tool factory which loads agent definitions from
workflow_definitions (DB-backed AgentDefinitionBody) and delegates to
spawn_agent_impl for execution.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import (
    IsolationContext,
)
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody, AgentWorkflows

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════
# _load_agent_body (DB lookup)
# ═══════════════════════════════════════════════════════════════════════


class TestLoadAgentBody:
    """_load_agent_body loads from workflow_definitions."""

    @pytest.fixture
    def db(self, tmp_path) -> LocalDatabase:
        db_path = tmp_path / "test_load_agent.db"
        database = LocalDatabase(db_path)
        run_migrations(database)
        return database

    @pytest.fixture
    def manager(self, db: LocalDatabase) -> LocalWorkflowDefinitionManager:
        return LocalWorkflowDefinitionManager(db)

    def test_loads_existing_agent(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body

        body = AgentDefinitionBody(
            name="test-dev-load",
            description="Developer agent",
            instructions="Write clean code.",
            provider="claude",
            model="claude-sonnet-4-6",
            isolation="worktree",
            base_branch="main",
            timeout=120.0,
            max_turns=15,
            workflows=AgentWorkflows(rules=["require-task-before-edit", "require-commit"]),
        )
        manager.create(
            name=body.name,
            definition_json=body.model_dump_json(),
            workflow_type="agent",
            description=body.description,
            enabled=True,
        )

        result = _load_agent_body("test-dev-load", db)
        assert result is not None
        assert result.name == "test-dev-load"
        assert result.provider == "claude"
        assert result.model == "claude-sonnet-4-6"
        assert result.isolation == "worktree"
        assert result.workflows.rules == ["require-task-before-edit", "require-commit"]

    def test_returns_none_for_missing_agent(self, db: LocalDatabase) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body

        result = _load_agent_body("nonexistent-agent", db)
        assert result is None

    def test_returns_none_for_none_db(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body

        result = _load_agent_body("any-agent", None)
        assert result is None

    def test_ignores_non_agent_types(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body

        manager.create(
            name="test-rule-not-agent",
            definition_json=json.dumps({"event": "before_tool", "effect": {"type": "block"}}),
            workflow_type="rule",
        )

        result = _load_agent_body("test-rule-not-agent", db)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# create_spawn_agent_registry
# ═══════════════════════════════════════════════════════════════════════


class TestCreateSpawnAgentRegistry:
    """Tests for create_spawn_agent_registry factory function."""

    def test_creates_registry_with_correct_name(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        runner = MagicMock()
        registry = create_spawn_agent_registry(runner)

        assert registry.name == "gobby-spawn-agent"

    def test_registers_spawn_agent_tool(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        runner = MagicMock()
        registry = create_spawn_agent_registry(runner)

        assert registry.get_schema("spawn_agent") is not None


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent defaults
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentDefaults:
    """Tests for spawn_agent with default values."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.mark.asyncio
    async def test_spawn_agent_defaults_to_default_agent(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = AgentDefinitionBody(
            name="default",
            provider="claude",
        )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ) as mock_load,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                },
            )

            # Verify "default" agent was loaded
            assert mock_load.call_args[0][0] == "default"
            assert result["success"] is True


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent isolation
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentIsolation:
    """Tests for spawn_agent isolation parameter."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.fixture
    def agent_body(self):
        return AgentDefinitionBody(
            name="default",
            provider="claude",
        )

    @pytest.mark.asyncio
    async def test_spawn_agent_current_uses_current_handler(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "isolation": "none",
                },
            )

            mock_get_handler.assert_called_once()
            call_args = mock_get_handler.call_args
            assert call_args[0][0] == "none"
            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_spawn_agent_worktree_creates_worktree(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(
            mock_runner,
            worktree_storage=MagicMock(),
            git_manager=MagicMock(),
            db=MagicMock(),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(
                    cwd="/tmp/worktrees/branch",
                    branch_name="test-branch",
                    worktree_id="wt-123",
                    isolation_type="worktree",
                )
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "isolation": "worktree",
                },
            )

            mock_get_handler.assert_called_once()
            call_args = mock_get_handler.call_args
            assert call_args[0][0] == "worktree"
            assert result["success"] is True
            assert result["worktree_id"] == "wt-123"

    @pytest.mark.asyncio
    async def test_spawn_agent_clone_creates_clone(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(
            mock_runner,
            clone_storage=MagicMock(),
            clone_manager=MagicMock(),
            db=MagicMock(),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(
                    cwd="/tmp/clones/branch",
                    branch_name="test-branch",
                    clone_id="clone-123",
                    isolation_type="clone",
                )
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "isolation": "clone",
                },
            )

            mock_get_handler.assert_called_once()
            call_args = mock_get_handler.call_args
            assert call_args[0][0] == "clone"
            assert result["success"] is True
            assert result["clone_id"] == "clone-123"


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent param overrides
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentParamOverrides:
    """Tests for tool params overriding agent definition values."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.mark.asyncio
    async def test_tool_params_override_agent_definition(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = AgentDefinitionBody(
            name="default",
            provider="claude",
        )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                },
            )

            assert result["success"] is True


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent task resolution
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentTaskResolution:
    """Tests for task_id resolution formats."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        return runner

    @pytest.fixture
    def agent_body(self):
        return AgentDefinitionBody(
            name="default",
            provider="claude",
        )

    @pytest.mark.asyncio
    async def test_task_id_supports_hash_n_format(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.title = "Implement feature"
        mock_task.seq_num = 6100
        mock_task.id = "uuid-123"
        mock_task_manager.get_task.return_value = mock_task

        registry = create_spawn_agent_registry(
            mock_runner,
            task_manager=mock_task_manager,
            db=MagicMock(),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.resolve_task_id_for_mcp"
            ) as mock_resolve,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_resolve.return_value = "uuid-123"

            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "task_id": "#6100",
                },
            )

            mock_resolve.assert_called_once()
            assert result["success"] is True


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent sandbox defaults
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentSandbox:
    """Tests for daemon-owned agent sandbox defaults."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.fixture
    def agent_body(self):
        return AgentDefinitionBody(
            name="default",
            provider="claude",
        )

    @pytest.mark.asyncio
    async def test_agent_sandbox_defaults_come_from_daemon_config(
        self, mock_runner, agent_body
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(
            mock_runner,
            db=MagicMock(),
            daemon_config=MagicMock(
                agent_sandbox=MagicMock(
                    enabled=False,
                    extra_read_paths=["/tmp/agent-read"],
                    extra_write_paths=["/tmp/agent-write"],
                )
            ),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Test prompt"
            mock_get_handler.return_value = mock_handler

            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                },
            )

            assert result["success"] is True
            spawn_request = mock_execute.call_args[0][0]
            assert spawn_request.sandbox_config is not None
            assert spawn_request.sandbox_config.enabled is False
            assert spawn_request.sandbox_config.extra_read_paths == ["/tmp/agent-read"]
            assert spawn_request.sandbox_config.extra_write_paths == ["/tmp/agent-write"]

    @pytest.mark.asyncio
    async def test_spawn_agent_schema_no_longer_exposes_sandbox_knobs(
        self, mock_runner, agent_body
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        schema = registry.get_schema("spawn_agent")

        assert schema is not None
        properties = schema["inputSchema"]["properties"]
        assert "sandbox" not in properties
        assert "sandbox_mode" not in properties
        assert "sandbox_allow_network" not in properties
        assert "sandbox_extra_paths" not in properties


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent pre-registration
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentPreRegistration:
    """Tests for agent registry pre-registration before execute_spawn."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.fixture
    def agent_body(self):
        return AgentDefinitionBody(
            name="default",
            provider="claude",
        )

    @pytest.mark.asyncio
    async def test_agent_db_record_created_during_spawn(self, mock_runner, agent_body):
        """Test that agent run DB record is created during spawn and updated after."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.update_child_session = MagicMock()
        mock_runner.run_storage.update_runtime = MagicMock()

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
            ) as mock_execute,
        ):
            mock_ctx.return_value = {"id": "proj-123", "project_path": "/path"}
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="ghostty",
                tmux_session_name=None,
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is True
            # After successful spawn, child_session_id should be updated in DB
            mock_runner.run_storage.update_child_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_failed_on_spawn_failure(self, mock_runner, agent_body):
        """Test that agent run is marked as failed in DB on spawn failure."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_runner.run_storage = MagicMock()
        mock_runner.run_storage.has_active_run_for_task.return_value = False
        mock_runner.run_storage.fail = MagicMock()

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {"id": "proj-123", "project_path": "/path"}
            mock_execute.return_value = MagicMock(
                success=False,
                error="Terminal not found",
                child_session_id=None,
            )

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is False
            # DB should mark the run as failed
            mock_runner.run_storage.fail.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent agent not found
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentNotFound:
    """Tests for agent not found behavior."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.mark.asyncio
    async def test_returns_error_for_missing_non_default_agent(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
            return_value=None,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test",
                    "parent_session_id": "parent-789",
                    "agent": "nonexistent",
                },
            )

            assert result["success"] is False
            assert "not found" in result["error"].lower()


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent prompt preamble
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentPromptPreamble:
    """Tests for prompt handling — preamble is injected via hooks, not prompt."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.mark.asyncio
    async def test_prompt_passed_without_preamble(self, mock_runner) -> None:
        """Preamble is injected via session_start hooks, not prepended to prompt."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = AgentDefinitionBody(
            name="dev",
            role="Backend developer",
            instructions="Write clean code.",
        )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            await registry.call(
                "spawn_agent",
                {
                    "prompt": "Fix the bug",
                    "parent_session_id": "parent-789",
                },
            )

            # Prompt is passed through as-is; preamble injected via hooks
            spawn_request = mock_execute.call_args[0][0]
            assert spawn_request.prompt == "Fix the bug"


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent initial variables
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentPipelineInjection:
    """Tests for _assigned_pipeline injection when workflow resolves to PipelineDefinition."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.mark.asyncio
    async def test_assigned_pipeline_set_for_pipeline_workflow(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.workflows.definitions import PipelineDefinition

        agent_body = AgentDefinitionBody(
            name="pipeline-agent",
            workflows=AgentWorkflows(pipeline="my-pipeline"),
        )

        mock_pipeline_def = MagicMock(spec=PipelineDefinition)

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch("gobby.workflows.loader.WorkflowLoader") as mock_wf_loader_cls,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_loader_instance = MagicMock()
            mock_loader_instance.load_workflow_sync.return_value = mock_pipeline_def
            mock_wf_loader_cls.return_value = mock_loader_instance

            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            await registry.call(
                "spawn_agent",
                {
                    "prompt": "Run pipeline",
                    "parent_session_id": "parent-789",
                },
            )

            spawn_request = mock_execute.call_args[0][0]
            assert spawn_request.initial_variables["_assigned_pipeline"] == "my-pipeline"

    @pytest.mark.asyncio
    async def test_assigned_pipeline_not_set_for_non_pipeline_workflow(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
        from gobby.workflows.definitions import WorkflowDefinition

        agent_body = AgentDefinitionBody(
            name="step-agent",
            workflows=AgentWorkflows(pipeline="my-workflow"),
        )

        mock_workflow_def = MagicMock(spec=WorkflowDefinition)

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch("gobby.workflows.loader.WorkflowLoader") as mock_wf_loader_cls,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_loader_instance = MagicMock()
            mock_loader_instance.load_workflow_sync.return_value = mock_workflow_def
            mock_wf_loader_cls.return_value = mock_loader_instance

            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            await registry.call(
                "spawn_agent",
                {
                    "prompt": "Run workflow",
                    "parent_session_id": "parent-789",
                },
            )

            spawn_request = mock_execute.call_args[0][0]
            assert "_assigned_pipeline" not in spawn_request.initial_variables


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent initial variables
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentStepVariables:
    """Tests for initial_variables (_agent_type, _agent_rules) from agent definition."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.mark.asyncio
    async def test_agent_type_set_in_initial_variables(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = AgentDefinitionBody(
            name="qa-agent",
            workflows=AgentWorkflows(rules=["no-code-writing"]),
        )

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-123",
                child_session_id="child-456",
                status="pending",
            )

            await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test it",
                    "parent_session_id": "parent-789",
                },
            )

            spawn_request = mock_execute.call_args[0][0]
            assert spawn_request.initial_variables["_agent_type"] == "qa-agent"
            assert spawn_request.initial_variables["_agent_rules"] == ["no-code-writing"]


# ═══════════════════════════════════════════════════════════════════════
# dispatch_batch isolation parity
# ═══════════════════════════════════════════════════════════════════════


class TestDispatchBatchIsolationParity:
    """Tests that dispatch_batch forwards clone/isolation params to spawn_agent."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.fixture
    def agent_body(self):
        return AgentDefinitionBody(
            name="developer",
            provider="claude",
        )

    @pytest.mark.asyncio
    async def test_dispatch_batch_forwards_clone_params(self, mock_runner, agent_body) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        mock_clone_storage = MagicMock()
        mock_clone = MagicMock()
        # Use /tmp which always exists, so clone path validation passes
        mock_clone.clone_path = "/tmp"
        mock_clone.branch_name = "feat-9981"
        mock_clone_storage.get.return_value = mock_clone

        registry = create_spawn_agent_registry(
            mock_runner,
            clone_storage=mock_clone_storage,
            clone_manager=MagicMock(),
            db=MagicMock(),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context"
            ) as mock_factory_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            project_ctx = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_factory_ctx.return_value = project_ctx
            mock_ctx.return_value = project_ctx
            spawn_result = MagicMock()
            spawn_result.success = True
            spawn_result.run_id = "run-123"
            spawn_result.child_session_id = "child-456"
            spawn_result.status = "pending"
            spawn_result.pid = None
            spawn_result.terminal_type = None
            spawn_result.tmux_session_name = None
            spawn_result.process = None
            spawn_result.error = None
            spawn_result.message = None
            mock_execute.return_value = spawn_result

            suggestions = [
                {"ref": "#9981", "id": "task-uuid-1", "title": "Add clone parity"},
            ]

            result = await registry.call(
                "dispatch_batch",
                {
                    "suggestions": suggestions,
                    "agent": "developer",
                    "clone_id": "clone-abc",
                    "isolation": "clone",
                    "branch_name": "feat-9981",
                    "base_branch": "0.2.28",
                    "parent_session_id": "parent-789",
                },
            )

            assert result["dispatched"] == 1
            assert result["results"][0]["success"] is True

            # Verify clone_id was forwarded — clone_storage.get was called with it
            mock_clone_storage.get.assert_called_once_with("clone-abc")

    @pytest.mark.asyncio
    async def test_dispatch_batch_without_isolation_params(self, mock_runner, agent_body) -> None:
        """dispatch_batch still works when no isolation params are provided (backwards compat)."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context"
            ) as mock_factory_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            project_ctx = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_factory_ctx.return_value = project_ctx
            mock_ctx.return_value = project_ctx
            spawn_result = MagicMock()
            spawn_result.success = True
            spawn_result.run_id = "run-456"
            spawn_result.child_session_id = "child-789"
            spawn_result.status = "pending"
            spawn_result.pid = None
            spawn_result.terminal_type = None
            spawn_result.tmux_session_name = None
            spawn_result.process = None
            spawn_result.error = None
            spawn_result.message = None
            mock_execute.return_value = spawn_result

            suggestions = [
                {"ref": "#100", "id": "task-1", "title": "Task one"},
            ]

            result = await registry.call(
                "dispatch_batch",
                {
                    "suggestions": suggestions,
                    "agent": "developer",
                    "parent_session_id": "parent-789",
                },
            )

            assert result["dispatched"] == 1
            assert result["results"][0]["success"] is True


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent dedup (idempotent)
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentDedup:
    """Tests for idempotent dedup when agent already running for a task."""

    @pytest.mark.asyncio
    async def test_dedup_returns_success_when_agent_already_running(self) -> None:
        """Dedup check should return success=True (not False) when agent already active."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = True

        active_run = MagicMock()
        active_run.id = "existing-run-456"
        runner.run_storage.get_active_run_for_task.return_value = active_run

        agent_body = AgentDefinitionBody(
            name="default",
            provider="claude",
        )

        mock_task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.title = "Test task"
        mock_task.seq_num = 100
        mock_task.id = "task-uuid-123"
        mock_task_manager.get_task.return_value = mock_task

        registry = create_spawn_agent_registry(
            runner,
            task_manager=mock_task_manager,
            db=MagicMock(),
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context"
            ) as mock_ctx,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.resolve_task_id_for_mcp"
            ) as mock_resolve,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_resolve.return_value = "task-uuid-123"

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "task_id": "#100",
                },
            )

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["run_id"] == "existing-run-456"
        assert "already running" in result["message"]


# ═══════════════════════════════════════════════════════════════════════
# spawn_agent_impl error branches
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentImplErrorBranches:
    """Tests for spawn_agent_impl error paths not covered by factory tests."""

    @pytest.mark.asyncio
    async def test_no_project_context_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value=None,
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
            )
            assert result["success"] is False
            assert "project context" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_no_project_id_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
            )
            assert result["success"] is False
            assert "project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_no_parent_session_id_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
            )
            assert result["success"] is False
            assert "parent_session_id" in result["error"]

    @pytest.mark.asyncio
    async def test_cannot_spawn_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (False, "Max depth reached", 5)

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
            )
            assert result["success"] is False
            assert "Max depth" in result["error"]

    @pytest.mark.asyncio
    async def test_worktree_id_not_found_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        worktree_storage = MagicMock()
        worktree_storage.get.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                worktree_id="wt-missing",
                worktree_storage=worktree_storage,
            )
            assert result["success"] is False
            assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_worktree_dir_missing_cleans_up(self, tmp_path) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        mock_wt = MagicMock()
        mock_wt.id = "wt-1"
        mock_wt.worktree_path = str(tmp_path / "nonexistent_dir")
        mock_wt.branch_name = "test-branch"

        worktree_storage = MagicMock()
        worktree_storage.get.return_value = mock_wt

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                worktree_id="wt-1",
                worktree_storage=worktree_storage,
            )
            assert result["success"] is False
            assert "missing" in result["error"].lower()
            worktree_storage.delete.assert_called_once_with("wt-1")

    @pytest.mark.asyncio
    async def test_clone_id_not_found_returns_error(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        clone_storage = MagicMock()
        clone_storage.get.return_value = None

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                clone_id="clone-missing",
                clone_storage=clone_storage,
            )
            assert result["success"] is False
            assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_clone_dir_missing_cleans_up(self, tmp_path) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        mock_clone = MagicMock()
        mock_clone.id = "clone-1"
        mock_clone.clone_path = str(tmp_path / "nonexistent_clone")
        mock_clone.branch_name = "test-branch"

        clone_storage = MagicMock()
        clone_storage.get.return_value = mock_clone

        with patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "proj-1", "project_path": "/path"},
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                clone_id="clone-1",
                clone_storage=clone_storage,
            )
            assert result["success"] is False
            assert "missing" in result["error"].lower()
            clone_storage.delete.assert_called_once_with("clone-1")

    @pytest.mark.asyncio
    async def test_prepare_environment_failure(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()

        mock_handler = MagicMock()
        mock_handler.prepare_environment = AsyncMock(side_effect=RuntimeError("git error"))
        mock_handler.cleanup_environment = AsyncMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={"id": "proj-1", "project_path": "/path"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler",
                return_value=mock_handler,
            ),
        ):
            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
            )
            assert result["success"] is False
            assert "prepare environment" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_timeout_zero_treated_as_none(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "ok", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False
        runner.run_storage.update_child_session = MagicMock()
        runner.run_storage.update_runtime = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={"id": "proj-1", "project_path": "/path"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_handler_fn,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            handler = MagicMock()
            handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/path"))
            handler.build_context_prompt.return_value = "test"
            mock_handler_fn.return_value = handler

            mock_execute.return_value = MagicMock(
                success=True,
                child_session_id="c-1",
                status="ok",
                pid=1,
                terminal_type=None,
                tmux_session_name=None,
                message="ok",
                process=None,
            )

            result = await spawn_agent_impl(
                prompt="test",
                runner=runner,
                parent_session_id="sess-1",
                timeout=0,
            )
            assert result["success"] is True


# ═══════════════════════════════════════════════════════════════════════
# Fallback agent on provider failure
# ═══════════════════════════════════════════════════════════════════════


class TestFallbackAgent:
    """Tests for fallback_agent provider rotation in the spawn factory."""

    @pytest.fixture
    def db(self, tmp_path) -> LocalDatabase:
        db_path = tmp_path / "test_fallback.db"
        database = LocalDatabase(db_path)
        run_migrations(database)
        return database

    @pytest.fixture
    def manager(self, db: LocalDatabase) -> LocalWorkflowDefinitionManager:
        return LocalWorkflowDefinitionManager(db)

    def _create_agent(
        self,
        manager: LocalWorkflowDefinitionManager,
        name: str,
        provider: str = "claude",
        model: str | None = None,
        fallback_agent: str | None = None,
    ) -> AgentDefinitionBody:
        body = AgentDefinitionBody(
            name=name,
            provider=provider,
            model=model,
            fallback_agent=fallback_agent,
        )
        manager.create(
            name=body.name,
            definition_json=body.model_dump_json(),
            workflow_type="agent",
            enabled=True,
        )
        return body

    @pytest.mark.asyncio
    async def test_fallback_triggered_on_provider_failure(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When primary provider has failed, factory loads fallback agent."""
        self._create_agent(manager, "dev-gemini", provider="gemini", fallback_agent="dev-claude")
        self._create_agent(manager, "dev-claude", provider="claude", model="opus")

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.agents.provider_rotation.get_failed_providers_for_task",
                return_value=["gemini"],
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-1"},
            ) as mock_impl,
        ):
            from gobby.mcp_proxy.tools.spawn_agent._factory import (
                create_spawn_agent_registry,
            )

            registry = create_spawn_agent_registry(runner, db=db)
            tool_fn = registry.get_tool("spawn_agent")
            assert tool_fn is not None

            await tool_fn(
                prompt="fix the bug",
                agent="dev-gemini",
                task_id="task-123",
            )

            # Should have been called with fallback agent's body
            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-claude"
            assert call_kwargs["agent_body"].provider == "claude"
            assert call_kwargs["agent_body"].model == "opus"

    @pytest.mark.asyncio
    async def test_no_fallback_when_provider_not_failed(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When primary provider has NOT failed, use primary agent."""
        self._create_agent(manager, "dev-gemini2", provider="gemini", fallback_agent="dev-claude2")
        self._create_agent(manager, "dev-claude2", provider="claude", model="opus")

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.agents.provider_rotation.get_failed_providers_for_task",
                return_value=[],  # No failures
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-1"},
            ) as mock_impl,
        ):
            from gobby.mcp_proxy.tools.spawn_agent._factory import (
                create_spawn_agent_registry,
            )

            registry = create_spawn_agent_registry(runner, db=db)
            tool_fn = registry.get_tool("spawn_agent")

            await tool_fn(
                prompt="fix the bug",
                agent="dev-gemini2",
                task_id="task-123",
            )

            # Should keep primary agent
            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-gemini2"
            assert call_kwargs["agent_body"].provider == "gemini"

    @pytest.mark.asyncio
    async def test_no_fallback_when_no_fallback_agent_set(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Agent without fallback_agent doesn't attempt rotation."""
        self._create_agent(manager, "dev-solo", provider="gemini")  # no fallback

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-1"},
            ) as mock_impl,
        ):
            from gobby.mcp_proxy.tools.spawn_agent._factory import (
                create_spawn_agent_registry,
            )

            registry = create_spawn_agent_registry(runner, db=db)
            tool_fn = registry.get_tool("spawn_agent")

            await tool_fn(
                prompt="fix the bug",
                agent="dev-solo",
                task_id="task-123",
            )

            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-solo"

    @pytest.mark.asyncio
    async def test_no_fallback_when_explicit_provider_override(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Explicit provider= param skips fallback (caller chose the provider)."""
        self._create_agent(manager, "dev-explicit", provider="gemini", fallback_agent="dev-fb")
        self._create_agent(manager, "dev-fb", provider="claude")

        runner = MagicMock()

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-1"},
            ) as mock_impl,
        ):
            from gobby.mcp_proxy.tools.spawn_agent._factory import (
                create_spawn_agent_registry,
            )

            registry = create_spawn_agent_registry(runner, db=db)
            tool_fn = registry.get_tool("spawn_agent")

            await tool_fn(
                prompt="fix the bug",
                agent="dev-explicit",
                provider="gemini",  # explicit override
                task_id="task-123",
            )

            # Should NOT fall back — caller explicitly chose gemini
            call_kwargs = mock_impl.call_args.kwargs
            assert call_kwargs["agent_body"].name == "dev-explicit"

    def test_fallback_agent_field_roundtrip(self) -> None:
        """AgentDefinitionBody with fallback_agent serializes/deserializes."""
        body = AgentDefinitionBody(
            name="test-fb",
            provider="gemini",
            fallback_agent="test-fb-claude",
        )
        dumped = body.model_dump_json()
        loaded = AgentDefinitionBody.model_validate_json(dumped)
        assert loaded.fallback_agent == "test-fb-claude"

    def test_fallback_agent_defaults_to_none(self) -> None:
        """Old JSON without fallback_agent deserializes to None."""
        old_json = '{"name": "legacy-agent", "provider": "claude"}'
        loaded = AgentDefinitionBody.model_validate_json(old_json)
        assert loaded.fallback_agent is None
