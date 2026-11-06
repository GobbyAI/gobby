"""Tests for terminal agent runtime cleanup."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.daemon_resume_keys import DISPATCH_LEASE_HELD_KEY
from gobby.storage.sessions import SessionManager
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
            agent_name="fixture-definition-76c4b50c",
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


@pytest.mark.parametrize(
    ("terminal_reason", "held_spawn_lease", "preseed_mark", "expects_resume_mark"),
    [
        ("daemon_stop", True, False, True),
        ("daemon_stop", False, False, False),
        ("daemon_stop", False, True, False),
        ("user_cancelled", True, False, False),
    ],
)
def test_cleanup_marks_daemon_parked_run_that_held_its_spawn_lease(
    temp_db: Any,
    sample_project: dict[str, Any],
    terminal_reason: str,
    held_spawn_lease: bool,
    preseed_mark: bool,
    expects_resume_mark: bool,
) -> None:
    machine_id = "21000000-0000-4000-8000-000000000001"
    parent = SessionManager(temp_db).register(
        external_id="parent-lease-mark",
        machine_id=machine_id,
        source="test",
        project_id=sample_project["id"],
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Parked spawn lease task",
        validation_criteria=(
            "Daemon-stop cleanup records on the parked run whether it held its spawn lease."
        ),
    )
    runs = LocalAgentRunManager(temp_db)
    run = runs.create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="work",
        task_id=task.id,
        machine_id=machine_id,
    )
    if preseed_mark:
        # Resume copies the predecessor's metadata, so a mark can arrive without a lease.
        runs.merge_resume_metadata(run.id, {DISPATCH_LEASE_HELD_KEY: True})
    mutex = TaskDispatchMutexManager(temp_db)
    if held_spawn_lease:
        assert mutex.acquire_mutex(
            task.id,
            holder=f"spawn-agent:{run.id}",
            kind="spawn_agent",
            ttl_seconds=600,
            run_id=run.id,
        )

    result = cleanup_agent_runtime_state(
        temp_db,
        run_id=run.id,
        child_session_id=None,
        terminal_reason=terminal_reason,
    )

    refreshed = runs.get(run.id)
    assert refreshed is not None
    marked = bool((refreshed.resume_metadata_json or {}).get(DISPATCH_LEASE_HELD_KEY))
    assert marked is expects_resume_mark
    assert result.dispatch_mutex_rows == (1 if held_spawn_lease else 0)
    assert result.errors == ()
    assert mutex.get_mutex(task.id) is None
