"""Red tests for runtime dispatch mutex wrapper."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_acquire_link_release_round_trip(temp_db, sample_project) -> None:
    from gobby.dispatch.mutex import RuntimeDispatchMutex
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Runtime mutex",
        validation_criteria="The dispatch mutex round trip completes without leaking.",
    )
    storage = TaskDispatchMutexManager(temp_db)

    with RuntimeDispatchMutex(
        storage,
        task_id=task.id,
        holder="dispatcher",
        action_kind="spawn_agent",
        ttl_seconds=30,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    ) as mutex:
        mutex.attach("ac314d27-4314-5fe3-a0ab-01645086e137")
        assert storage.get_mutex(task.id).run_id == "ac314d27-4314-5fe3-a0ab-01645086e137"

    attached = storage.get_mutex(task.id)
    assert attached is not None
    assert attached.run_id == "ac314d27-4314-5fe3-a0ab-01645086e137"
    assert RuntimeDispatchMutex.force_release_for_run(storage, attached.run_id) == 1


def test_detach_on_terminal_no_leak(temp_db, sample_project) -> None:
    from gobby.dispatch.mutex import RuntimeDispatchMutex
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Runtime terminal mutex",
        validation_criteria="Terminal cleanup removes the dispatch mutex.",
    )
    storage = TaskDispatchMutexManager(temp_db)

    with RuntimeDispatchMutex(storage, task.id, "dispatcher", "spawn_agent", 30) as mutex:
        mutex.attach("a0a76c4c-539f-51e2-b9b3-6bd1333cbd45")
        assert (
            RuntimeDispatchMutex.force_release_for_run(
                storage, "a0a76c4c-539f-51e2-b9b3-6bd1333cbd45"
            )
            == 1
        )

    assert storage.get_mutex(task.id) is None


def test_runtime_mutex_uses_unique_holder_token_per_acquisition(temp_db, sample_project) -> None:
    from gobby.dispatch.mutex import DispatchMutexUnavailableError, RuntimeDispatchMutex
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Unique runtime mutex holder",
        validation_criteria="Concurrent acquisitions use unique holder tokens.",
    )
    storage = TaskDispatchMutexManager(temp_db)
    barrier = Barrier(2)

    def acquire(action_kind: str) -> bool:
        mutex = RuntimeDispatchMutex(storage, task.id, "dispatcher", action_kind, 30)
        acquired = False
        barrier.wait()
        try:
            mutex.__enter__()
            acquired = True
            lease = storage.get_mutex(task.id)
            assert lease is not None
            assert lease.lease_holder.startswith("dispatcher:")
        except DispatchMutexUnavailableError:
            pass
        finally:
            barrier.wait()
            if acquired:
                assert mutex.release()
        return acquired

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, ("heartbeat", "spawn_agent")))

    assert results.count(True) == 1
