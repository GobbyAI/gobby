"""Factory-level spawn_agent tool tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


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


class TestSpawnAgentDefaults:
    """Tests for spawn_agent with default values."""

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


class TestSpawnAgentParamOverrides:
    """Tests for tool params overriding agent definition values."""

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


class TestSpawnAgentTaskResolution:
    """Tests for task_id resolution formats."""

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


class TestSpawnAgentSandbox:
    """Tests for daemon-owned agent sandbox defaults."""

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


class TestSpawnAgentNotFound:
    """Tests for agent not found behavior."""

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


class TestSpawnAgentPromptPreamble:
    """Tests for prompt handling — preamble is injected via hooks, not prompt."""

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
