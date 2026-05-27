"""Dispatcher wake coverage for automated stage and close transitions."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _capture_dispatch_schedules(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def schedule(db, *, project_id: str, reason: str, services=None) -> bool:
        calls.append((project_id, reason))
        return True

    monkeypatch.setattr("gobby.build.dispatch_tick.schedule_dispatcher_tick_for_project", schedule)
    return calls


def test_submit_for_review_schedules_direct_dispatch_tick(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.tasks import LocalTaskManager, StageManifestSpec

    calls = _capture_dispatch_schedules(monkeypatch)
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
    calls.clear()

    manager.stage_states.submit_for_review(task.id, "development", by_session_id="worker")

    assert calls == [(sample_project["id"], "task_change")]
    from gobby.storage.cron import CronJobStorage

    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None


def test_close_task_schedules_direct_dispatch_tick(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.tasks import LocalTaskManager

    calls = _capture_dispatch_schedules(monkeypatch)
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="No-review leaf",
        category="code",
        task_type="feature",
    )
    manager.update_task(task.id, allow_automation=True, assigned_agent="backend-developer")

    manager.close_task(task.id, reason="completed")

    assert calls == [(sample_project["id"], "task_change")]
    from gobby.storage.cron import CronJobStorage

    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None


def test_dispatcher_wake_respects_stopped_automation(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.storage.tasks import LocalTaskManager, StageManifestSpec

    calls = _capture_dispatch_schedules(monkeypatch)
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

    manager.stage_states.submit_for_review(task.id, "development", by_session_id="worker")

    from gobby.storage.cron import CronJobStorage

    assert calls == []
    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None
