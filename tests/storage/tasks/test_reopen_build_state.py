"""Reopen behavior for inactive build-controlled tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from tests.storage.tasks._stage_test_helpers import (
    initialize_manifest,
    lifecycle_events,
    set_stage_state,
    spec,
    stage_row,
)

pytestmark = pytest.mark.unit


def _task(temp_db, sample_project):
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Reopen build state",
        category="code",
        task_type="task",
    )
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    return manager, task


def test_reopen_resets_inactive_open_non_ready_stage(temp_db, sample_project) -> None:
    manager, task = _task(temp_db, sample_project)
    set_stage_state(temp_db, task.id, "development", "in_progress", work_attempt_count=1)
    manager.update_task(task.id, validation_fail_count=2, dispatch_failure_count=3)

    reopened = manager.reopen_task(task.id, reason="manual recovery")

    row = stage_row(temp_db, task.id, "development")
    assert row["state"] == "ready"
    assert row["work_attempt_count"] == 1
    assert reopened.claimed_by_session_id is None
    assert reopened.validation_fail_count == 0
    assert reopened.dispatch_failure_count == 0
    assert lifecycle_events(temp_db, task.id)[-1]["reason"] == "reopen_task"


def test_reopen_blocks_allow_automation_with_build_stop_instruction(
    temp_db,
    sample_project,
) -> None:
    manager, task = _task(temp_db, sample_project)
    manager.update_task(task.id, allow_automation=True)

    with pytest.raises(
        ValueError,
        match=(
            rf"Task #{task.seq_num} is controlled by active build automation\. "
            rf"Run gobby build stop #{task.seq_num} before reopening it\."
        ),
    ):
        manager.reopen_task(task.id)


def test_reopen_blocks_active_dispatch_mutex(temp_db, sample_project) -> None:
    manager, task = _task(temp_db, sample_project)
    set_stage_state(temp_db, task.id, "development", "in_progress")
    TaskDispatchMutexManager(temp_db).acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        ttl_seconds=300,
    )

    with pytest.raises(ValueError, match="controlled by active build automation"):
        manager.reopen_task(task.id)


def test_reopen_ignores_expired_dispatch_mutex(temp_db, sample_project) -> None:
    manager, task = _task(temp_db, sample_project)
    set_stage_state(temp_db, task.id, "development", "in_progress")
    expired = datetime.now(UTC) - timedelta(seconds=60)
    TaskDispatchMutexManager(temp_db).acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        ttl_seconds=1,
        now=expired,
    )

    manager.reopen_task(task.id)

    assert stage_row(temp_db, task.id, "development")["state"] == "ready"


def test_reopen_blocks_active_agent_run(temp_db, sample_project) -> None:
    manager, task = _task(temp_db, sample_project)
    set_stage_state(temp_db, task.id, "development", "in_progress")
    parent = SessionManager(temp_db).register(
        external_id="reopen-parent",
        machine_id="machine",
        source="codex",
        project_id=sample_project["id"],
    )
    LocalAgentRunManager(temp_db).create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="work",
        task_id=task.id,
        run_id="run-reopen-active",
    )

    with pytest.raises(ValueError, match="controlled by active build automation"):
        manager.reopen_task(task.id)
