"""Agent workflow completion releases dispatch runtime state."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_agent_workflow_completion_clears_mutex_and_workflow_instance(
    temp_db,
    sample_project,
) -> None:
    temp_db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        (
            "child-session",
            "ext-child-session",
            "machine-1",
            "codex",
            sample_project["id"],
        ),
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Workflow-owned task",
    )
    mutex = TaskDispatchMutexManager(temp_db)
    mutex.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        run_id="run-456",
        ttl_seconds=300,
    )
    instance_manager = WorkflowInstanceManager(temp_db)
    instance_manager.save_instance(
        WorkflowInstance(
            id="wf-456",
            session_id="child-session",
            workflow_name="tech-writer-steps",
            current_step="terminate",
            step_entered_at=datetime.now(UTC),
        )
    )

    runner = MagicMock()
    runner.run_storage.get_by_session.return_value = SimpleNamespace(id="run-456")
    engine = RuleEngine(db=temp_db, runner=runner)

    with patch(
        "gobby.workflows.engine.enforcement.complete_and_notify_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ) as complete:
        await engine._complete_agent_workflow_run("child-session", "tech-writer-steps")

    complete.assert_awaited_once()
    assert mutex.get_mutex(task.id) is None
    assert instance_manager.get_active_instances("child-session") == []
