"""Red tests for runtime dispatch mutex wrapper."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_acquire_link_release_round_trip(temp_db, sample_project) -> None:
    from gobby.dispatch.mutex import RuntimeDispatchMutex

    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Runtime mutex",
    )
    storage = TaskDispatchMutexManager(temp_db)
    storage.ensure_table()

    with RuntimeDispatchMutex(
        storage,
        task_id=task.id,
        holder="dispatcher",
        action_kind="spawn_agent",
        ttl_seconds=30,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    ) as mutex:
        mutex.attach("run-1")
        assert storage.get_mutex(task.id).run_id == "run-1"

    assert storage.get_mutex(task.id) is None


def test_detach_on_terminal_no_leak(temp_db, sample_project) -> None:
    from gobby.dispatch.mutex import RuntimeDispatchMutex

    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Runtime terminal mutex",
    )
    storage = TaskDispatchMutexManager(temp_db)
    storage.ensure_table()

    with RuntimeDispatchMutex(storage, task.id, "dispatcher", "spawn_agent", 30) as mutex:
        mutex.attach("run-terminal")
        assert RuntimeDispatchMutex.force_release_for_run(storage, "run-terminal") == 1

    assert storage.get_mutex(task.id) is None

