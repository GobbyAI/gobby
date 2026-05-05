"""Tests for terminal agent runtime cleanup."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.state_manager import WorkflowInstanceManager

pytestmark = pytest.mark.unit


def test_cleanup_agent_runtime_state_releases_mutex_and_workflow(
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
        title="Agent-owned task",
    )
    mutex = TaskDispatchMutexManager(temp_db)
    mutex.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        run_id="run-123",
        ttl_seconds=300,
    )
    instance_manager = WorkflowInstanceManager(temp_db)
    instance_manager.save_instance(
        WorkflowInstance(
            id="wf-123",
            session_id="child-session",
            workflow_name="tech-writer-steps",
            current_step="implement",
            step_entered_at=datetime.now(UTC),
        )
    )

    result = cleanup_agent_runtime_state(
        temp_db,
        run_id="run-123",
        child_session_id="child-session",
    )

    assert result.dispatch_mutex_rows == 1
    assert result.workflow_instance_rows == 1
    assert result.errors == ()
    assert mutex.get_mutex(task.id) is None
    assert instance_manager.get_active_instances("child-session") == []
