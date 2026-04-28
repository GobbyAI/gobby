"""Red tests for task dispatch mutex storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _manager_class() -> type:
    import gobby.storage.tasks as task_module

    return task_module.TaskDispatchMutexManager


def test_acquire_mutex_respects_live_holder_and_allows_owner_refresh(
    temp_db,
    sample_project,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Mutex task",
    )
    manager = _manager_class()(temp_db)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert manager.acquire_mutex(
        task.id,
        holder="state-dispatcher:1",
        kind="lifecycle",
        run_id=None,
        ttl_seconds=60,
        now=now,
    )
    assert not manager.acquire_mutex(
        task.id,
        holder="state-dispatcher:2",
        kind="lifecycle",
        run_id=None,
        ttl_seconds=60,
        now=now + timedelta(seconds=10),
    )
    assert manager.acquire_mutex(
        task.id,
        holder="state-dispatcher:1",
        kind="lifecycle",
        run_id=None,
        ttl_seconds=120,
        now=now + timedelta(seconds=10),
    )

    mutex = manager.get_mutex(task.id)
    assert mutex is not None
    assert mutex.lease_holder == "state-dispatcher:1"
    assert mutex.action_kind == "lifecycle"


def test_expired_mutex_can_be_reacquired_by_new_holder(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Expired mutex",
    )
    manager = _manager_class()(temp_db)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert manager.acquire_mutex(
        task.id,
        holder="old-holder",
        kind="worktree",
        run_id=None,
        ttl_seconds=5,
        now=now,
    )
    assert manager.acquire_mutex(
        task.id,
        holder="new-holder",
        kind="worktree",
        run_id=None,
        ttl_seconds=30,
        now=now + timedelta(seconds=6),
    )

    assert manager.get_mutex(task.id).lease_holder == "new-holder"


def test_release_mutex_only_releases_matching_holder(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Release mutex",
    )
    manager = _manager_class()(temp_db)

    assert manager.acquire_mutex(
        task.id,
        holder="owner",
        kind="field",
        run_id=None,
        ttl_seconds=30,
    )
    assert manager.release_mutex(task.id, holder="other") is False
    assert manager.get_mutex(task.id) is not None

    assert manager.release_mutex(task.id, holder="owner") is True
    assert manager.get_mutex(task.id) is None


def test_sweep_expired_removes_only_stale_leases(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    stale = task_manager.create_task(project_id=sample_project["id"], title="Stale")
    fresh = task_manager.create_task(project_id=sample_project["id"], title="Fresh")
    manager = _manager_class()(temp_db)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    manager.acquire_mutex(stale.id, holder="old", kind="field", ttl_seconds=5, now=now, run_id=None)
    manager.acquire_mutex(
        fresh.id,
        holder="new",
        kind="field",
        ttl_seconds=60,
        now=now,
        run_id=None,
    )

    assert manager.sweep_expired(now=now + timedelta(seconds=10)) == 1
    assert manager.get_mutex(stale.id) is None
    assert manager.get_mutex(fresh.id) is not None
