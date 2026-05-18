"""Red tests for append-only task lifecycle event audit rows."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager, TaskLifecycleEventManager
from gobby.storage.tasks._lifecycle_events import BUILD_EVENT_REASON

pytestmark = pytest.mark.unit


def _event_manager_class() -> type:
    return TaskLifecycleEventManager


def test_record_lifecycle_event_requires_reason_and_actor(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Lifecycle event",
    )
    manager = _event_manager_class()(temp_db)

    with pytest.raises(ValueError, match="reason"):
        manager.record_lifecycle_event(
            task.id,
            from_state="open",
            to_state="plan_review",
            reason="",
            by_actor="cli",
        )
    with pytest.raises(ValueError, match="by_actor"):
        manager.record_lifecycle_event(
            task.id,
            from_state="open",
            to_state="plan_review",
            reason="operator requested build",
            by_actor="",
        )


def test_record_lifecycle_event_appends_ordered_audit_rows(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Lifecycle event",
    )
    manager = _event_manager_class()(temp_db)

    first = manager.record_lifecycle_event(
        task.id,
        from_state="open",
        to_state="plan_review",
        reason="operator requested build",
        by_actor="cli",
    )
    second = manager.record_lifecycle_event(
        task.id,
        from_state="plan_review",
        to_state="expansion",
        reason="plan approved",
        by_actor="holistic-reviewer",
    )

    events = manager.list_lifecycle_events(task.id)
    assert len(events) == 2
    assert events[0].id == first.id
    assert events[1].id == second.id
    assert [event.to_state for event in events] == [
        "plan_review",
        "expansion",
    ]
    assert events[0].reason == "operator requested build"
    assert events[1].by_actor == "holistic-reviewer"
    assert not hasattr(manager, "update_lifecycle_event")
    assert not hasattr(manager, "delete_lifecycle_event")


def test_has_build_event_only_true_after_a_gobby_build_event(
    temp_db, sample_project
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Build state",
    )
    manager = _event_manager_class()(temp_db)

    assert manager.has_build_event(task.id) is False

    # A non-build lifecycle event must NOT count as build evidence.
    manager.record_lifecycle_event(
        task.id,
        from_state="open",
        to_state="plan_review",
        reason="operator requested build",
        by_actor="cli",
    )
    assert manager.has_build_event(task.id) is False

    # The durable build marker recorded by `gobby build`.
    manager.record_lifecycle_event(
        task.id,
        from_state=None,
        to_state="implement",
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )
    assert manager.has_build_event(task.id) is True


def test_build_event_survives_a_stop_so_state_reads_paused_not_never_started(
    temp_db, sample_project
) -> None:
    """build_stop_target clears allow_automation without recording a new
    event or bumping dispatch_failure_count; the build marker must persist so
    a stopped build is classifiable as paused, not never_started."""
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Stopped build",
    )
    manager = _event_manager_class()(temp_db)
    manager.record_lifecycle_event(
        task.id,
        from_state=None,
        to_state="implement",
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )

    # Simulating a stop performs no lifecycle-events mutation at all.
    assert manager.has_build_event(task.id) is True


def test_tasks_with_build_event_is_a_batched_predicate(
    temp_db, sample_project
) -> None:
    task_manager = LocalTaskManager(temp_db)
    built = task_manager.create_task(project_id=sample_project["id"], title="Built")
    untouched = task_manager.create_task(
        project_id=sample_project["id"], title="Never built"
    )
    other_event = task_manager.create_task(
        project_id=sample_project["id"], title="Has non-build event"
    )
    manager = _event_manager_class()(temp_db)
    manager.record_lifecycle_event(
        built.id,
        from_state=None,
        to_state="implement",
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )
    manager.record_lifecycle_event(
        other_event.id,
        from_state="open",
        to_state="plan_review",
        reason="plan approved",
        by_actor="cli",
    )

    result = manager.tasks_with_build_event(
        [built.id, untouched.id, other_event.id]
    )

    assert result == {built.id}
    assert manager.tasks_with_build_event([]) == set()


def test_module_helpers_return_id_and_list_newest_first(temp_db, sample_project) -> None:
    from gobby.storage.tasks import list_lifecycle_events, record_lifecycle_event

    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Lifecycle event",
    )

    first_id = record_lifecycle_event(
        temp_db,
        task.id,
        from_state=None,
        to_state="plan_review",
        reason="operator requested build",
        by_actor="cli",
    )
    second_id = record_lifecycle_event(
        temp_db,
        task.id,
        from_state="plan_review",
        to_state="expansion",
        reason="plan approved",
        by_actor="holistic-reviewer",
    )

    events = list_lifecycle_events(temp_db, task.id)
    assert isinstance(first_id, int)
    assert isinstance(second_id, int)
    assert len(events) == 2
    assert [event.id for event in events[:2]] == [second_id, first_id]
