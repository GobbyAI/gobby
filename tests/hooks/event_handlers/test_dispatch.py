"""Red tests for dispatch mutex event handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.tasks import LocalTaskManager, Task
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

pytestmark = pytest.mark.unit


def _stage_pipeline_task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    review_policy: str = "required",
    requested: str = "in_progress",
) -> tuple[LocalTaskManager, Task, TaskDispatchMutexManager]:
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
    from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Pipeline stage",
        validation_criteria="Test task completion is observable.",
    )
    temp_db.execute(
        "UPDATE task_stages_registry SET review_policy = %s WHERE name = 'expansion'",
        (review_policy,),
    )
    initialize_manifest(temp_db, task.id, [spec("expansion", 0)])
    set_stage_state(temp_db, task.id, "expansion", requested)
    storage = TaskDispatchMutexManager(temp_db)
    storage.ensure_table()
    storage.acquire_mutex(task.id, holder="dispatcher", kind="heartbeat", ttl_seconds=30)
    temp_db.execute(
        """
        UPDATE task_dispatch_mutex
           SET run_id = %s, action_kind = %s
         WHERE task_id = %s
        """,
        ("796ce97e-38ee-508a-bdc0-f3ce2dded342", "stage-pipeline:expansion", task.id),
    )
    return manager, task, storage


def test_pipeline_completed_submits_required_stage_for_review(temp_db, sample_project) -> None:
    from gobby.hooks.event_handlers import _dispatch

    manager, task, storage = _stage_pipeline_task(temp_db, sample_project)

    _dispatch.on_pipeline_completed(
        {"execution_id": "796ce97e-38ee-508a-bdc0-f3ce2dded342"}, db=temp_db, storage=storage
    )

    assert manager.stage_states.get(task.id, "expansion").state == "needs_review"
    assert storage.get_mutex(task.id) is None


def test_pipeline_completed_preserves_review_when_continuation_scheduling_fails(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    manager, task, storage = _stage_pipeline_task(temp_db, sample_project)

    def fail_schedule(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(
        _dispatch,
        "schedule_dispatcher_continuation_for_task",
        fail_schedule,
    )

    updated = _dispatch.on_pipeline_completed(
        {"execution_id": "796ce97e-38ee-508a-bdc0-f3ce2dded342"},
        db=temp_db,
        storage=storage,
    )

    assert updated is not None
    assert manager.stage_states.get(task.id, "expansion").state == "needs_review"
    assert storage.get_mutex(task.id) is None
    warning = next(
        record
        for record in caplog.records
        if record.message == "Failed to schedule dispatcher continuation after stage review"
    )
    assert vars(warning)["task_id"] == task.id
    assert vars(warning)["stage_name"] == "expansion"
    assert vars(warning)["run_id"] == "796ce97e-38ee-508a-bdc0-f3ce2dded342"


def test_pipeline_transition_holds_mutex_until_stage_update(
    temp_db,
    sample_project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    manager, task, storage = _stage_pipeline_task(temp_db, sample_project)
    stage_states = manager.stage_states
    submit_for_review = stage_states.submit_for_review
    transition_observed = False

    def assert_mutex_held(*args: Any, **kwargs: Any) -> object:
        nonlocal transition_observed
        transition_observed = True
        assert not storage.acquire_mutex(
            task.id,
            holder="competing-dispatcher",
            kind="heartbeat",
            ttl_seconds=30,
        )
        return submit_for_review(*args, **kwargs)

    monkeypatch.setattr(stage_states, "submit_for_review", assert_mutex_held)
    monkeypatch.setattr(_dispatch, "_stage_states", lambda _db: stage_states)

    _dispatch.on_pipeline_completed(
        {"execution_id": "796ce97e-38ee-508a-bdc0-f3ce2dded342"},
        db=temp_db,
        storage=storage,
    )

    assert transition_observed
    assert storage.acquire_mutex(
        task.id,
        holder="competing-dispatcher",
        kind="heartbeat",
        ttl_seconds=30,
    )


def test_pipeline_failed_returns_stage_to_ready(temp_db, sample_project) -> None:
    from gobby.hooks.event_handlers import _dispatch

    manager, task, storage = _stage_pipeline_task(temp_db, sample_project)

    _dispatch.on_pipeline_failed(
        {"execution_id": "796ce97e-38ee-508a-bdc0-f3ce2dded342", "error": "boom"},
        db=temp_db,
        storage=storage,
    )

    stage = manager.stage_states.get(task.id, "expansion")
    assert stage.state == "ready"
    assert storage.get_mutex(task.id) is None


def test_pipeline_cancelled_escalates_stage_and_releases_mutex(temp_db, sample_project) -> None:
    from gobby.hooks.event_handlers import _dispatch

    manager, task, storage = _stage_pipeline_task(temp_db, sample_project)

    _dispatch.on_pipeline_cancelled(
        {"execution_id": "796ce97e-38ee-508a-bdc0-f3ce2dded342"}, db=temp_db, storage=storage
    )

    stage = manager.stage_states.get(task.id, "expansion")
    assert stage.state == "ready"
    assert manager.get_task(task.id).is_escalated is True
    assert storage.get_mutex(task.id) is None


def test_pipeline_failed_illegal_transition_is_ignored_after_mutex_release(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    manager, task, storage = _stage_pipeline_task(
        temp_db,
        sample_project,
        requested="ready",
    )

    result = _dispatch.on_pipeline_failed(
        {"execution_id": "796ce97e-38ee-508a-bdc0-f3ce2dded342", "error": "boom"},
        db=temp_db,
        storage=storage,
    )

    assert result is None
    stage = manager.stage_states.get(task.id, "expansion")
    assert stage is not None
    assert stage.state == "ready"
    assert storage.get_mutex(task.id) is None
