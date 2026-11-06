"""A run records that its own spawn attached the task lease, and releases that record."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import TaskSpawnLease
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.daemon_resume_keys import SPAWN_LEASE_RUN_KEY
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000001"


def _task_and_run(temp_db: Any, sample_project: dict[str, Any]) -> tuple[Any, Any]:
    parent = SessionManager(temp_db).register(
        external_id="parent-spawn-lease-owner",
        machine_id=MACHINE_ID,
        source="test",
        project_id=sample_project["id"],
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Spawn lease owner",
        validation_criteria="The run records its own spawn lease and releases the record.",
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="work",
        task_id=task.id,
        machine_id=MACHINE_ID,
    )
    return task, run


def test_attach_records_owner_marker_and_normal_release_clears_it(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    task, run = _task_and_run(temp_db, sample_project)
    lease = TaskSpawnLease(db=temp_db, task_id=task.id)
    assert lease.acquire() is None
    assert lease.attach(run.id) is None

    runs = LocalAgentRunManager(temp_db)
    attached = runs.get(run.id)
    assert attached is not None
    assert (attached.resume_metadata_json or {}).get(SPAWN_LEASE_RUN_KEY) == run.id

    result = cleanup_agent_runtime_state(
        temp_db,
        run_id=run.id,
        child_session_id=None,
        terminal_reason="user_cancelled",
    )

    assert result.errors == ()
    assert TaskDispatchMutexManager(temp_db).get_mutex(task.id) is None
    released = runs.get(run.id)
    assert released is not None
    assert (released.resume_metadata_json or {}).get(SPAWN_LEASE_RUN_KEY) is None


def test_spawn_that_reused_a_held_mutex_writes_no_owner_marker(
    temp_db: Any, sample_project: dict[str, Any]
) -> None:
    task, run = _task_and_run(temp_db, sample_project)
    lease = TaskSpawnLease(db=temp_db, task_id=task.id, held_mutex=object())
    assert lease.acquire() is None
    assert lease.attach(run.id) is None

    refreshed = LocalAgentRunManager(temp_db).get(run.id)
    assert refreshed is not None
    assert SPAWN_LEASE_RUN_KEY not in (refreshed.resume_metadata_json or {})
