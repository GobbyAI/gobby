"""Spawn-agent idempotent deduplication tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.storage.hub.protocol import HubDatabase
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
        active_run.status = "running"
        active_run.child_session_id = "child-456"
        active_run.parent_session_id = "parent-789"
        active_run.task_id = "task-uuid-123"
        active_run.agent_name = "backend-developer"
        runner.run_storage.list_active_global.return_value = [active_run]
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
                "id": "11111111-1111-4111-8111-111111110123",
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
        assert result["status"] == "running"
        assert result["child_session_id"] == "child-456"
        assert result["parent_session_id"] == "parent-789"
        assert result["task_id"] == "task-uuid-123"
        assert result["agent_name"] == "backend-developer"
        assert "already running" in result["message"]

    @pytest.mark.asyncio
    async def test_spawn_refuses_closed_task_before_launch(self) -> None:
        """Closed tasks are refused before isolation or terminal launch."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False

        agent_body = AgentDefinitionBody(
            name="default",
            provider="claude",
        )

        mock_task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.title = "Closed task"
        mock_task.seq_num = 101
        mock_task.id = "task-uuid-closed"
        mock_task.additional_skills = None
        mock_task.closed_at = "2026-05-22T00:00:00+00:00"
        mock_task.stages = []
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
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new_callable=AsyncMock,
            ) as mock_execute,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_resolve.return_value = "task-uuid-closed"

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Test prompt",
                    "parent_session_id": "parent-789",
                    "task_id": "#101",
                    "isolation": "worktree",
                },
            )

        assert result["success"] is False
        assert result["skipped"] is True
        assert result["task_id"] == "task-uuid-closed"
        assert "not actionable" in result["error"]
        mock_execute.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("closed_at", "escalated_at", "expect_spawn"),
        [
            ("2026-05-22T00:00:00+00:00", None, True),
            (None, "2026-05-22T01:00:00+00:00", False),
            (
                "2026-05-22T00:00:00+00:00",
                "2026-05-22T01:00:00+00:00",
                False,
            ),
        ],
        ids=["closed-reviewable", "open-escalated", "closed-escalated"],
    )
    async def test_allow_closed_task_permits_review_spawn_unless_escalated(
        self, db: HubDatabase, closed_at: str | None, escalated_at: str | None, expect_spawn: bool
    ) -> None:
        """allow_closed_task admits closed tasks for review; open escalated tasks still refuse."""
        from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

        runner = MagicMock()
        runner.can_spawn.return_value = (True, "Can spawn", 0)
        runner._child_session_manager = MagicMock()
        runner.run_storage.has_active_run_for_task.return_value = False

        agent_body = AgentDefinitionBody(name="epic-reviewer", provider="claude")

        mock_task_manager = MagicMock()
        mock_task = MagicMock()
        mock_task.title = "Closed epic"
        mock_task.seq_num = 102
        mock_task.id = "11111111-1111-4111-8111-111111110222"
        mock_task.additional_skills = None
        mock_task.closed_at = closed_at
        mock_task.escalated_at = escalated_at
        mock_task.is_escalated = escalated_at is not None
        mock_task.stages = []
        mock_task_manager.get_task.return_value = mock_task

        registry = create_spawn_agent_registry(
            runner,
            task_manager=mock_task_manager,
            db=db,
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
                "gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn",
                new_callable=AsyncMock,
            ) as mock_execute,
            patch(
                "gobby.mcp_proxy.tools.spawn_agent._implementation.TaskSpawnLease"
            ) as mock_lease_cls,
        ):
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
                "project_path": "/path/to/project",
            }
            mock_resolve.return_value = "11111111-1111-4111-8111-111111110222"
            mock_lease_cls.return_value.acquire.return_value = None
            mock_lease_cls.return_value.attach.return_value = None
            spawn_result = MagicMock()
            spawn_result.success = True
            spawn_result.terminal_type = "headless"
            spawn_result.child_session_id = "child-review-1"
            spawn_result.error = None
            mock_execute.return_value = spawn_result

            result = await registry.call(
                "spawn_agent",
                {
                    "prompt": "Post-hoc epic review",
                    "parent_session_id": "parent-789",
                    "task_id": "#102",
                    "allow_closed_task": True,
                },
            )

        if expect_spawn:
            mock_execute.assert_awaited_once()
            assert result["success"] is True
        else:
            mock_execute.assert_not_awaited()
            assert result["success"] is False
            assert "not actionable" in result["error"]

    @pytest.mark.asyncio
    async def test_merge_worker_spawn_ignores_parent_merge_orchestrator_run(
        self, db: HubDatabase
    ) -> None:
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
        runner.run_storage.list_active_global.return_value = [parent_run]
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
        mock_task.stages = []
        mock_task.closed_at = None
        claimed_task = MagicMock()
        claimed_task.claimed_by_session_id = "child-merge-worker"
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.claim_task.return_value = claimed_task

        registry = create_spawn_agent_registry(
            runner,
            task_manager=mock_task_manager,
            db=db,
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
            patch("gobby.mcp_proxy.tools.spawn_agent._implementation.TaskSpawnLease") as lease_cls,
        ):
            lease = lease_cls.return_value
            lease.acquire.return_value = None
            lease.attach.return_value = None
            lease.release_unattached.return_value = None
            mock_ctx.return_value = {
                "id": "11111111-1111-4111-8111-111111110123",
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
                terminal_id=None,
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
        runner.run_storage.list_active_global.return_value = [active_worker]
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
                "id": "11111111-1111-4111-8111-111111110123",
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
