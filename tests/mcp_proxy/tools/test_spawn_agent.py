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
            mode="terminal",
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
        assert result.mode == "terminal"
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
            mode="terminal",
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
            mode="terminal",
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

        # Agent definition says mode=autonomous
        agent_body = AgentDefinitionBody(
            name="default",
            provider="claude",
            mode="autonomous",
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

            # Override with mode=terminal via tool param
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "mode": "terminal",
                },
            )

            assert result["success"] is True
            execute_call = mock_execute.call_args
            assert execute_call[0][0].mode == "terminal"


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
        return runner

    @pytest.fixture
    def agent_body(self):
        return AgentDefinitionBody(
            name="default",
            provider="claude",
            mode="terminal",
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
# spawn_agent sandbox
# ═══════════════════════════════════════════════════════════════════════


class TestSpawnAgentSandbox:
    """Tests for spawn_agent sandbox parameters."""

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
            mode="terminal",
        )

    @pytest.mark.asyncio
    async def test_sandbox_params_create_sandbox_config(self, mock_runner, agent_body) -> None:
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
                    "sandbox": True,
                    "sandbox_mode": "restrictive",
                    "sandbox_allow_network": False,
                },
            )

            assert result["success"] is True
            spawn_request = mock_execute.call_args[0][0]
            assert spawn_request.sandbox_config is not None
            assert spawn_request.sandbox_config.enabled is True
            assert spawn_request.sandbox_config.mode == "restrictive"
            assert spawn_request.sandbox_config.allow_network is False

    @pytest.mark.asyncio
    async def test_sandbox_extra_paths_passed_to_config(self, mock_runner, agent_body):
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
                    "sandbox": True,
                    "sandbox_extra_paths": ["/tmp/data", "/opt/tools"],
                },
            )

            assert result["success"] is True
            spawn_request = mock_execute.call_args[0][0]
            assert spawn_request.sandbox_config is not None
            assert "/tmp/data" in spawn_request.sandbox_config.extra_write_paths
            assert "/opt/tools" in spawn_request.sandbox_config.extra_write_paths

    @pytest.mark.asyncio
    async def test_sandbox_disabled_when_param_false(self, mock_runner, agent_body):
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
                    "sandbox": False,
                },
            )

            assert result["success"] is True
            spawn_request = mock_execute.call_args[0][0]
            if spawn_request.sandbox_config is not None:
                assert spawn_request.sandbox_config.enabled is False


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
            mode="terminal",
        )

    @pytest.mark.asyncio
    async def test_agent_registered_before_spawn(self, mock_runner, agent_body):
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        call_order: list[str] = []
        mock_agent_registry = MagicMock()
        mock_agent_registry.add.side_effect = lambda agent: call_order.append(f"add:{agent.run_id}")

        async def fake_execute(req):
            call_order.append("execute_spawn")
            return MagicMock(
                success=True,
                run_id=req.run_id,
                child_session_id="child-456",
                status="pending",
                pid=12345,
                terminal_type="ghostty",
                tmux_session_name=None,
                message="Spawned",
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
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                side_effect=fake_execute,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_running_agent_registry",
                return_value=mock_agent_registry,
            ),
        ):
            mock_ctx.return_value = {"id": "proj-123", "project_path": "/path"}

            result = await registry.call(
                "spawn_agent",
                {"prompt": "Test", "parent_session_id": "parent-789"},
            )

            assert result["success"] is True
            assert len(call_order) == 3  # add (pre-reg), execute_spawn, add (update)
            assert call_order[0].startswith("add:")
            assert call_order[1] == "execute_spawn"
            assert call_order[2].startswith("add:")
            mock_agent_registry.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_removed_on_spawn_failure(self, mock_runner, agent_body):
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        mock_agent_registry = MagicMock()
        pre_registered_run_id: list[str] = []

        def track_add(agent):
            pre_registered_run_id.append(agent.run_id)

        mock_agent_registry.add.side_effect = track_add

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
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_running_agent_registry",
                return_value=mock_agent_registry,
            ),
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
            assert len(pre_registered_run_id) == 1
            mock_agent_registry.remove.assert_called_once_with(
                pre_registered_run_id[0], status="failed"
            )


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
    """Tests for prompt preamble composition from agent definition."""

    @pytest.fixture
    def mock_runner(self):
        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        return runner

    @pytest.mark.asyncio
    async def test_preamble_prepended_to_prompt(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = AgentDefinitionBody(
            name="dev",
            role="Backend developer",
            instructions="Write clean code.",
            mode="terminal",
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

            # Check the prompt passed to spawn_agent_impl includes preamble
            spawn_request = mock_execute.call_args[0][0]
            assert "Backend developer" in spawn_request.prompt
            assert "Write clean code" in spawn_request.prompt
            assert "Fix the bug" in spawn_request.prompt


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
            mode="terminal",
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
            mode="terminal",
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
            mode="terminal",
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
