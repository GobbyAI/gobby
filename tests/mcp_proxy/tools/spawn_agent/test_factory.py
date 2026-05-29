"""Factory-level spawn_agent tool tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody, WorkflowStep

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

    def test_spawn_agent_schema_includes_notify_parent_on_completion_default_true(
        self, mock_runner
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())
        schema = registry.get_schema("spawn_agent")

        assert schema is not None
        properties = schema["inputSchema"]["properties"]
        assert properties["notify_parent_on_completion"] == {
            "type": "boolean",
            "default": True,
        }
        assert "notify_parent_on_completion" not in schema["inputSchema"]["required"]


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

    @pytest.mark.asyncio
    async def test_spawn_agent_notify_parent_on_completion_false_skips_subscription(
        self, mock_runner
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = AgentDefinitionBody(name="default", provider="claude")
        completion_registry = MagicMock()
        db = MagicMock()
        registry = create_spawn_agent_registry(
            mock_runner,
            db=db,
            completion_registry=completion_registry,
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-123", "project_path": "/path/to/project"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-123"},
            ) as mock_spawn_impl,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.subscribe_agent_completion"
            ) as mock_subscribe,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "notify_parent_on_completion": False,
                },
            )

        assert result["success"] is True
        assert mock_spawn_impl.call_args.kwargs["parent_session_id"] == "parent-789"
        mock_subscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawn_agent_notify_parent_on_completion_defaults_to_subscribe(
        self, mock_runner
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = AgentDefinitionBody(name="default", provider="claude")
        completion_registry = MagicMock()
        db = MagicMock()
        registry = create_spawn_agent_registry(
            mock_runner,
            db=db,
            completion_registry=completion_registry,
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-123", "project_path": "/path/to/project"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True, "run_id": "run-123"},
            ) as mock_spawn_impl,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.subscribe_agent_completion"
            ) as mock_subscribe,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                },
            )

        assert result["success"] is True
        assert result["run_id"] == "run-123"
        assert mock_spawn_impl.call_args.kwargs["prompt"] == "Test prompt"
        assert mock_spawn_impl.call_args.kwargs["parent_session_id"] == "parent-789"
        mock_subscribe.assert_called_once_with(
            completion_registry=completion_registry,
            run_id="run-123",
            subscriber_session_id="parent-789",
            session_manager=None,
            db=db,
        )

    @pytest.mark.asyncio
    async def test_spawn_agent_derives_project_path_from_parent_session(
        self,
        mock_runner,
        db: HubDatabase,
        tmp_path: Path,
    ) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        project = LocalProjectManager(db).create(
            "spawn-parent-project",
            repo_path=str(tmp_path),
        )
        session_manager = MagicMock()
        session_manager.resolve_session_reference.return_value = "parent-uuid"
        session_manager.get.return_value = SimpleNamespace(project_id=project.id)
        agent_body = AgentDefinitionBody(
            name="spawn-memory-recall-helper",
            provider="claude",
        )
        registry = create_spawn_agent_registry(
            mock_runner,
            session_manager=session_manager,
            db=db,
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value=None,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ) as mock_load,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as mock_spawn_impl,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Recall relevant memory",
                    "agent": "spawn-memory-recall-helper",
                    "parent_session_id": "parent-ref",
                },
            )

        assert result["success"] is True
        assert mock_load.call_args.kwargs["project_id"] == project.id
        assert mock_spawn_impl.call_args.kwargs["parent_session_id"] == "parent-uuid"
        assert mock_spawn_impl.call_args.kwargs["project_path"] == str(tmp_path)


class TestSpawnAgentParamOverrides:
    """Tests for tool params overriding agent definition values."""

    async def _spawn_request_for(
        self,
        mock_runner,
        agent_body: AgentDefinitionBody,
        call_params: dict[str, object],
    ) -> Any:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-123", "project_path": "/path/to/project"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
                return_value={"id": "proj-123", "project_path": "/path/to/project"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
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

            params: dict[str, object] = {
                "prompt": "Test prompt",
                "parent_session_id": "parent-789",
            }
            params.update(call_params)
            result = await registry.call("spawn_agent", params)

            assert result["success"] is True
            return mock_execute.call_args[0][0]

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
            assert mock_execute.call_args[0][0].provider == "claude"

    @pytest.mark.asyncio
    async def test_provider_override_omits_agent_definition_model(self, mock_runner) -> None:
        agent_body = AgentDefinitionBody(
            name="merge-worker",
            provider="gemini",
            model="gemini-3.1-pro-preview",
        )

        spawn_request = await self._spawn_request_for(
            mock_runner,
            agent_body,
            {
                "agent": "merge-worker",
                "provider": "claude",
            },
        )

        assert spawn_request.provider == "claude"
        assert spawn_request.model is None

    @pytest.mark.asyncio
    async def test_provider_override_preserves_explicit_model(self, mock_runner) -> None:
        agent_body = AgentDefinitionBody(
            name="merge-worker",
            provider="gemini",
            model="gemini-3.1-pro-preview",
        )

        spawn_request = await self._spawn_request_for(
            mock_runner,
            agent_body,
            {
                "agent": "merge-worker",
                "provider": "claude",
                "model": "opus",
            },
        )

        assert spawn_request.provider == "claude"
        assert spawn_request.model == "opus"

    @pytest.mark.asyncio
    async def test_provider_override_blank_model_uses_provider_default(self, mock_runner) -> None:
        agent_body = AgentDefinitionBody(
            name="merge-worker",
            provider="gemini",
            model="gemini-3.1-pro-preview",
        )

        spawn_request = await self._spawn_request_for(
            mock_runner,
            agent_body,
            {
                "agent": "merge-worker",
                "provider": "claude",
                "model": "   ",
            },
        )

        assert spawn_request.provider == "claude"
        assert spawn_request.model is None

    @pytest.mark.asyncio
    async def test_no_provider_override_keeps_agent_definition_model(self, mock_runner) -> None:
        agent_body = AgentDefinitionBody(
            name="merge-worker",
            provider="gemini",
            model="gemini-3.1-pro-preview",
        )

        spawn_request = await self._spawn_request_for(
            mock_runner,
            agent_body,
            {
                "agent": "merge-worker",
            },
        )

        assert spawn_request.provider == "gemini"
        assert spawn_request.model == "gemini-3.1-pro-preview"

    @pytest.mark.asyncio
    async def test_model_prefix_infers_provider_without_provider_override(
        self, mock_runner
    ) -> None:
        agent_body = AgentDefinitionBody(
            name="merge-worker",
            provider="gemini",
            model="gemini-3.1-pro-preview",
        )

        spawn_request = await self._spawn_request_for(
            mock_runner,
            agent_body,
            {
                "agent": "merge-worker",
                "model": "claude/sonnet-4-6",
            },
        )

        assert spawn_request.provider == "claude"
        assert spawn_request.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_explicit_provider_rejects_mismatched_model_prefix(self, mock_runner) -> None:
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        agent_body = AgentDefinitionBody(
            name="merge-worker",
            provider="gemini",
            model="gemini-3.1-pro-preview",
        )
        registry = create_spawn_agent_registry(mock_runner, db=MagicMock())

        with (
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context",
                return_value={"id": "proj-123", "project_path": "/path/to/project"},
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
                return_value=agent_body,
            ),
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn"
            ) as mock_execute,
        ):
            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "agent": "merge-worker",
                    "provider": "gemini",
                    "model": "claude/sonnet-4-6",
                },
            )

        assert result["success"] is False
        assert "does not match explicit provider" in result["error"]
        mock_execute.assert_not_called()


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
            assert mock_resolve.call_count == 1
            assert mock_resolve.call_args is not None
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
            assert spawn_request.agent_name == "default"


class TestRegisterAgentStepWorkflow:
    """Regression tests for _register_agent_step_workflow self-healing behavior."""

    @pytest.fixture
    def db(self, temp_db: HubDatabase) -> HubDatabase:
        database = temp_db
        return database

    def test_self_heals_workflow_type_when_existing_row_is_corrupted(self, db: HubDatabase) -> None:
        """A pre-existing `<agent>-steps` row with workflow_type='pipeline' must be
        repaired to 'workflow' on the next spawn. Without this, a single corrupted
        row stays corrupted forever and breaks the loader on every restart.
        """
        from gobby.mcp_proxy.tools.spawn_agent._factory import (
            _register_agent_step_workflow,
        )

        mgr = LocalWorkflowDefinitionManager(db)
        # Seed a corrupted row exactly matching the live failure mode:
        # workflow_type='pipeline' but JSON body is a step workflow.
        mgr.create(
            name="rogue-agent-steps",
            definition_json=json.dumps({"name": "rogue-agent-steps", "type": "step"}),
            workflow_type="pipeline",
            source="agent",
            enabled=False,
        )

        body = AgentDefinitionBody(
            name="rogue-agent",
            steps=[WorkflowStep(name="claim")],
        )
        returned_name = _register_agent_step_workflow(body, db)

        assert returned_name == "rogue-agent-steps"
        repaired = mgr.get_by_name("rogue-agent-steps")
        assert repaired is not None
        assert repaired.workflow_type == "workflow"
        assert repaired.source == "agent"
        # Body now matches what the factory writes, not the seeded stub.
        body_json = json.loads(repaired.definition_json)
        assert body_json["type"] == "step"
        assert body_json["steps"][0]["name"] == "claim"
