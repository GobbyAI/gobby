"""Red tests for append-only task lifecycle event audit rows."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager, TaskLifecycleEventManager

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
        to_state="test_arch",
        reason="plan approved",
        by_actor="holistic-reviewer",
    )

    events = manager.list_lifecycle_events(task.id)
    assert [event.id for event in events] == [first.id, second.id]
    assert [event.to_state for event in events] == ["plan_review", "test_arch"]
    assert events[0].reason == "operator requested build"
    assert events[1].by_actor == "holistic-reviewer"
    assert not hasattr(manager, "update_lifecycle_event")
    assert not hasattr(manager, "delete_lifecycle_event")


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
        to_state="test_arch",
        reason="plan approved",
        by_actor="holistic-reviewer",
    )

    events = list_lifecycle_events(temp_db, task.id)
    assert isinstance(first_id, int)
    assert isinstance(second_id, int)
    assert [event.id for event in events] == [second_id, first_id]
