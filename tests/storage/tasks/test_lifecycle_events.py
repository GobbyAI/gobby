"""Red tests for append-only task lifecycle event audit rows."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _event_manager_class() -> type:
    import gobby.storage.tasks as task_module

    return task_module.TaskLifecycleEventManager


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
