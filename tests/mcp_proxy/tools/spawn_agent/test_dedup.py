"""Spawn-agent idempotent deduplication tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


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
        runner.run_storage.list_active.return_value = [active_run]
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

    @pytest.mark.asyncio
    async def test_merge_worker_spawn_ignores_parent_merge_orchestrator_run(self) -> None:
        """A merge orchestrator may spawn a same-task merge worker without hitting itself."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        runner.child_session_manager = runner._child_session_manager
        runner.run_storage.has_active_run_for_task.return_value = True

        parent_run = MagicMock()
        parent_run.id = "run-orchestrator"
        parent_run.agent_name = "merge-orchestrator"
        parent_run.child_session_id = "parent-merge-session"
        runner.run_storage.list_active.return_value = [parent_run]
        runner.run_storage.get_active_run_for_task.return_value = parent_run

        agent_body = AgentDefinitionBody(
            name="merge-worker",
            provider="claude",
        )

        mock_task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.title = "Merge task"
        mock_task.seq_num = 14084
        mock_task.id = "task-uuid-14084"
        mock_task.additional_skills = None
        mock_task.claimed_by_session_id = None
        mock_task.assignee = None
        mock_task.stages = []
        mock_task.closed_at = None
        claimed_task = MagicMock()
        claimed_task.claimed_by_session_id = "child-merge-worker"
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.claim_task.return_value = claimed_task

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
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
            ) as mock_get_handler,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new_callable=AsyncMock,
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "proj-123",
                "project_path": "/path/to/project",
            }
            mock_resolve.return_value = "task-uuid-14084"
            mock_handler = MagicMock()
            mock_handler.prepare_environment = AsyncMock(
                return_value=IsolationContext(cwd="/path/to/project")
            )
            mock_handler.build_context_prompt.return_value = "Merge prompt"
            mock_get_handler.return_value = mock_handler
            mock_execute.return_value = MagicMock(
                success=True,
                run_id="run-merge-worker",
                child_session_id="child-merge-worker",
                status="pending",
                pid=1234,
                terminal_type="ghostty",
                tmux_session_name=None,
                message="Spawned",
            )

            result = await registry.call(
                "spawn_agent",
                {
                    "agent": "merge-worker",
                    "prompt": "Merge prompt",
                    "parent_session_id": "parent-merge-session",
                    "task_id": "#14084",
                    "isolation": "none",
                },
            )

        assert result["success"] is True
        assert result.get("skipped") is not True
        mock_execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_merge_worker_spawn_dedups_existing_merge_worker_run(self) -> None:
        """The same-task exception does not allow duplicate active merge workers."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = True

        active_worker = MagicMock()
        active_worker.id = "run-existing-worker"
        active_worker.agent_name = "merge-worker"
        active_worker.child_session_id = "child-existing-worker"
        runner.run_storage.list_active.return_value = [active_worker]
        runner.run_storage.get_active_run_for_task.return_value = active_worker

        agent_body = AgentDefinitionBody(
            name="merge-worker",
            provider="claude",
        )

        mock_task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.title = "Merge task"
        mock_task.seq_num = 14084
        mock_task.id = "task-uuid-14084"
        mock_task.additional_skills = None
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
            mock_resolve.return_value = "task-uuid-14084"

            result = await registry.call(
                "spawn_agent",
                {
                    "agent": "merge-worker",
                    "prompt": "Merge prompt",
                    "parent_session_id": "parent-merge-session",
                    "task_id": "#14084",
                },
            )

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["run_id"] == "run-existing-worker"
