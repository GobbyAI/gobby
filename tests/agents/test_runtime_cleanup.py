"""Tests for terminal agent runtime cleanup."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("terminal_reason", "expected_workflow_rows"),
    [
        (None, 1),
        ("user_cancelled", 1),
        ("daemon_stop", 0),
    ],
)
def test_cleanup_agent_runtime_state_releases_mutex_and_conditionally_deletes_workflow(
    temp_db,
    sample_project,
    terminal_reason: str | None,
    expected_workflow_rows: int,
) -> None:
    temp_db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'active', NOW(), NOW())
        """,
        (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa2001",
            "ext-child-session",
            "21000000-0000-4000-8000-000000000001",
            "codex",
            sample_project["id"],
        ),
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Agent-owned task",
        validation_criteria=(
            "Agent runtime cleanup always releases its dispatch mutex and retains workflow state "
            "only for daemon-stop."
        ),
    )
    mutex = TaskDispatchMutexManager(temp_db)
    mutex.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd2004",
        ttl_seconds=300,
    )
    instance_manager = AgentStepInstanceManager(temp_db)
    instance_manager.save(
        make_step_instance(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa2001",
            agent_name="tech-writer",
            current_step="implement",
        )
    )

    result = cleanup_agent_runtime_state(
        temp_db,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd2004",
        child_session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa2001",
        terminal_reason=terminal_reason,
    )

    assert result.dispatch_mutex_rows == 1
    assert result.workflow_instance_rows == expected_workflow_rows
    assert result.errors == ()
    assert mutex.get_mutex(task.id) is None
    remaining = instance_manager.get_for_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaa2001")
    if terminal_reason == "daemon_stop":
        assert remaining is not None
        assert remaining.current_step == "implement"
    else:
        assert remaining is None
