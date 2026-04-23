"""Spawn-agent idempotent deduplication tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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
