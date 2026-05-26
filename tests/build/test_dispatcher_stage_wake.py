"""Dispatcher wake coverage for automated stage and close transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.unit


def _park_dispatcher(temp_db, project_id: str) -> str:
    from gobby.runner import DISPATCHER_CRON_JOB_NAME, install_dispatcher_cron_row
    from gobby.storage.cron import CronJobStorage

    job = install_dispatcher_cron_row(temp_db, project_id=project_id)
    storage = CronJobStorage(temp_db)
    parked = storage.park_system_job(job.id)
    assert parked is not None
    assert storage.get_job_by_name(DISPATCHER_CRON_JOB_NAME).next_run_at is None
    return job.id


def _assert_dispatcher_due_now(temp_db, job_id: str) -> None:
    from gobby.storage.cron import CronJobStorage

    job = CronJobStorage(temp_db).get_job(job_id)
    assert job is not None
    assert job.next_run_at is not None
    due_at = datetime.fromisoformat(job.next_run_at)
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    assert due_at <= datetime.now(UTC) + timedelta(seconds=1)


def test_submit_for_review_wakes_parked_dispatcher_cron(temp_db, sample_project) -> None:
    from gobby.storage.tasks import LocalTaskManager, StageManifestSpec

    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Leaf",
        category="code",
        task_type="feature",
    )
    manager.update_task(task.id, allow_automation=True, assigned_agent="backend-developer")
    manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("development", 0)],
        by_session_id=None,
    )
    manager.stage_states.start_stage(task.id, "development", by_session_id="worker")
    job_id = _park_dispatcher(temp_db, sample_project["id"])

    manager.stage_states.submit_for_review(task.id, "development", by_session_id="worker")

    _assert_dispatcher_due_now(temp_db, job_id)


def test_close_task_wakes_parked_dispatcher_cron(temp_db, sample_project) -> None:
    from gobby.storage.tasks import LocalTaskManager

    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="No-review leaf",
        category="code",
        task_type="feature",
    )
    manager.update_task(task.id, allow_automation=True, assigned_agent="backend-developer")
    job_id = _park_dispatcher(temp_db, sample_project["id"])

    manager.close_task(task.id, reason="completed")

    _assert_dispatcher_due_now(temp_db, job_id)


def test_dispatcher_wake_respects_stopped_automation(temp_db, sample_project) -> None:
    from gobby.storage.tasks import LocalTaskManager, StageManifestSpec

    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Stopped leaf",
        category="code",
        task_type="feature",
    )
    manager.update_task(task.id, allow_automation=False, assigned_agent="backend-developer")
    manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("development", 0)],
        by_session_id=None,
    )
    manager.stage_states.start_stage(task.id, "development", by_session_id="worker")
    job_id = _park_dispatcher(temp_db, sample_project["id"])

    manager.stage_states.submit_for_review(task.id, "development", by_session_id="worker")

    from gobby.storage.cron import CronJobStorage

    assert CronJobStorage(temp_db).get_job(job_id).next_run_at is None
