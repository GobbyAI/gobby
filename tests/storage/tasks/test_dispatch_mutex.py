"""Tests for task dispatch mutex storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _manager_class() -> type:
    import gobby.storage.tasks as task_module

    return task_module.TaskDispatchMutexManager


def test_acquire_release_round_trip(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
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
    assert datetime.fromisoformat(mutex.lease_until) == now + timedelta(seconds=130)

    assert manager.release_mutex(task.id, holder="state-dispatcher:1")
    assert manager.get_mutex(task.id) is None


def test_active_mutex_cannot_be_replaced_by_same_holder_with_different_run(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Run mutex task",
    )
    manager = _manager_class()(temp_db)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert manager.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        run_id="run-1",
        ttl_seconds=60,
        now=now,
    )
    assert manager.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        run_id=None,
        ttl_seconds=140,
        now=now + timedelta(seconds=15),
    )
    assert manager.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        run_id="run-1",
        ttl_seconds=120,
        now=now + timedelta(seconds=20),
    )
    assert not manager.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        run_id="run-2",
        ttl_seconds=300,
        now=now + timedelta(seconds=30),
    )

    mutex = manager.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id == "run-1"
    assert datetime.fromisoformat(mutex.lease_until) == now + timedelta(seconds=140)


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


def test_force_release(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Force release mutex",
    )
    manager = _manager_class()(temp_db)

    assert manager.acquire_mutex(
        task.id,
        holder="owner",
        kind="field",
        run_id=None,
        ttl_seconds=30,
    )
    assert manager.force_release(task.id) is True
    assert manager.get_mutex(task.id) is None
    assert manager.force_release(task.id) is False


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


def test_attach_run_id_links_run_to_lease(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Attach run id",
    )
    manager = _manager_class()(temp_db)

    assert manager.acquire_mutex(
        task.id,
        holder="owner",
        kind="agent",
        run_id=None,
        ttl_seconds=30,
    )

    assert manager.attach_run_id(task.id, "run-123") is True
    mutex = manager.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id == "run-123"
    assert manager.clear_by_run_id("run-123") == 1
    assert manager.get_mutex(task.id) is None


def test_refresh_mutex_for_run_extends_matching_lease_only(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(project_id=sample_project["id"], title="Refresh mutex")
    other = task_manager.create_task(project_id=sample_project["id"], title="Other mutex")
    manager = _manager_class()(temp_db)
    past = datetime(2026, 1, 1, tzinfo=UTC)
    refresh_at = past + timedelta(minutes=10)

    assert manager.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        run_id="run-123",
        ttl_seconds=60,
        now=past,
    )
    assert manager.acquire_mutex(
        other.id,
        holder="dispatcher",
        kind="heartbeat",
        run_id="run-456",
        ttl_seconds=60,
        now=past,
    )

    assert (
        manager.refresh_mutex_for_run(
            task.id,
            "wrong-run",
            lease_holder="dispatcher",
            ttl_seconds=600,
            now=refresh_at,
        )
        is False
    )
    assert (
        manager.refresh_mutex_for_run(
            task.id,
            "run-123",
            lease_holder="other-owner",
            ttl_seconds=600,
            now=refresh_at,
        )
        is False
    )
    assert (
        manager.refresh_mutex_for_run(
            task.id,
            "run-123",
            lease_holder="dispatcher",
            ttl_seconds=600,
            now=refresh_at,
        )
        is True
    )

    mutex = manager.get_mutex(task.id)
    assert mutex is not None
    assert datetime.fromisoformat(mutex.lease_until) == refresh_at + timedelta(seconds=600)
    assert mutex.lease_holder == "dispatcher"
    assert mutex.action_kind == "heartbeat"

    other_mutex = manager.get_mutex(other.id)
    assert other_mutex is not None
    assert datetime.fromisoformat(other_mutex.lease_until) == past + timedelta(seconds=60)


def test_ensure_table_creates_run_id_index(temp_db) -> None:
    manager = _manager_class()(temp_db)

    manager.ensure_table()

    indexes = {
        row["name"]
        for row in temp_db.fetchall(
            """
            SELECT indexname AS name
              FROM pg_indexes
             WHERE schemaname = current_schema()
               AND tablename = 'task_dispatch_mutex'
            """
        )
    }
    assert "idx_dispatch_mutex_run_id" in indexes


def test_sweep_expired(temp_db, sample_project) -> None:
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
