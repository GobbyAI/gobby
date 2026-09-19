"""A closed task takes no new children, and the guard holds under concurrency.

Epic #22527 closed and then collected twelve children, nine of them still open
(#22570). The close direction was already gated; creation and reparenting were
not, and a check without the parent row lock would only narrow the window
rather than shut it.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase, TaskSeqAllocation
from gobby.storage.tasks import (
    LocalTaskManager,
    ParentTaskClosedError,
    TaskClosedError,
    TaskHasOpenChildrenError,
)
from gobby.storage.tasks._creation import _create_task_in_transaction
from gobby.storage.tasks._stage_utils import _close_task_in_txn

pytestmark = pytest.mark.unit

CRITERIA = "Test task completion is observable."
BLOCKED_FOR_SECONDS = 1.0
JOIN_TIMEOUT_SECONDS = 10.0


def _open_parent(manager: LocalTaskManager, project_id: str) -> Any:
    return manager.create_task(
        project_id,
        "Parent epic",
        task_type="epic",
        validation_criteria=CRITERIA,
    )


def _closed_parent(manager: LocalTaskManager, project_id: str) -> Any:
    parent = _open_parent(manager, project_id)
    manager.close_task(parent.id, reason="completed")
    return manager.get_task(parent.id)


def test_create_under_closed_parent_is_refused(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    parent = _closed_parent(manager, sample_project["id"])

    with pytest.raises(ParentTaskClosedError) as excinfo:
        manager.create_task(
            sample_project["id"],
            "Late child",
            parent_task_id=parent.id,
            validation_criteria=CRITERIA,
        )

    assert excinfo.value.parent_task_id == parent.id
    assert excinfo.value.parent_ref == f"#{parent.seq_num}"
    assert f"#{parent.seq_num}" in str(excinfo.value)
    assert "Reopen it" in str(excinfo.value)

    children = temp_db.fetchall(
        "SELECT id FROM tasks WHERE parent_task_id = %s",
        (parent.id,),
    )
    assert children == []


def test_parent_closed_error_is_catchable_as_task_closed(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    parent = _closed_parent(manager, sample_project["id"])

    with pytest.raises(TaskClosedError):
        manager.create_task(
            sample_project["id"],
            "Late child",
            parent_task_id=parent.id,
            validation_criteria=CRITERIA,
        )


def test_create_under_open_parent_is_unaffected(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    parent = _open_parent(manager, sample_project["id"])

    child = manager.create_task(
        sample_project["id"],
        "Ordinary child",
        parent_task_id=parent.id,
        validation_criteria=CRITERIA,
    )

    assert child.parent_task_id == parent.id
    assert child.path_cache == f"{parent.seq_num}.{child.seq_num}"


def test_create_without_parent_is_unaffected(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)

    task = manager.create_task(
        sample_project["id"],
        "Root task",
        validation_criteria=CRITERIA,
    )

    assert task.parent_task_id is None
    assert task.path_cache == str(task.seq_num)


def test_reparent_onto_closed_parent_is_refused(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    closed = _closed_parent(manager, sample_project["id"])
    orphan = manager.create_task(
        sample_project["id"],
        "Existing work",
        validation_criteria=CRITERIA,
    )

    with pytest.raises(ParentTaskClosedError) as excinfo:
        manager.update_task(orphan.id, parent_task_id=closed.id)

    assert excinfo.value.parent_ref == f"#{closed.seq_num}"
    assert manager.get_task(orphan.id).parent_task_id is None


def test_reparent_onto_open_parent_is_unaffected(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    parent = _open_parent(manager, sample_project["id"])
    orphan = manager.create_task(
        sample_project["id"],
        "Existing work",
        validation_criteria=CRITERIA,
    )

    manager.update_task(orphan.id, parent_task_id=parent.id)

    assert manager.get_task(orphan.id).parent_task_id == parent.id


def test_reopening_a_leaf_reopens_its_auto_closed_parent(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Closing the last child auto-closes the epic, so reopening it must undo that.

    Otherwise every leaf reopen recreates #22527's shape: an open task under a
    closed parent, by the door create and reparent no longer leave open.
    """
    manager = LocalTaskManager(temp_db)
    parent = _open_parent(manager, sample_project["id"])
    child = manager.create_task(
        sample_project["id"],
        "Only leaf",
        parent_task_id=parent.id,
        validation_criteria=CRITERIA,
    )
    manager.close_task(child.id, reason="completed")
    assert manager.get_task(parent.id).closed_at is not None

    manager.reopen_task(child.id, reason="more work found")

    assert manager.get_task(child.id).closed_at is None
    assert manager.get_task(parent.id).closed_at is None


def test_reopening_a_leaf_reopens_the_whole_closed_ancestor_chain(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    grandparent = _open_parent(manager, sample_project["id"])
    parent = manager.create_task(
        sample_project["id"],
        "Middle epic",
        task_type="epic",
        parent_task_id=grandparent.id,
        validation_criteria=CRITERIA,
    )
    child = manager.create_task(
        sample_project["id"],
        "Only leaf",
        parent_task_id=parent.id,
        validation_criteria=CRITERIA,
    )
    manager.close_task(child.id, reason="completed")
    assert manager.get_task(grandparent.id).closed_at is not None

    manager.reopen_task(child.id, reason="more work found")

    assert manager.get_task(parent.id).closed_at is None
    assert manager.get_task(grandparent.id).closed_at is None


def test_reopening_a_leaf_leaves_an_open_parent_alone(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    parent = _open_parent(manager, sample_project["id"])
    closed_child = manager.create_task(
        sample_project["id"],
        "Finished leaf",
        parent_task_id=parent.id,
        validation_criteria=CRITERIA,
    )
    manager.create_task(
        sample_project["id"],
        "Leaf still open",
        parent_task_id=parent.id,
        validation_criteria=CRITERIA,
    )
    manager.close_task(closed_child.id, reason="completed")
    parent_before = manager.get_task(parent.id)
    assert parent_before.closed_at is None

    manager.reopen_task(closed_child.id, reason="more work found")

    parent_after = manager.get_task(parent.id)
    assert parent_after.closed_at is None
    assert parent_after.updated_at == parent_before.updated_at


def test_create_waits_for_a_concurrent_parent_close_and_is_refused(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """A create that starts mid-close blocks on the parent row, then refuses.

    Without the FOR UPDATE in _lock_open_parent the create reads the parent's
    pre-close row and inserts, leaving an open child under a closed parent.
    """
    manager = LocalTaskManager(temp_db)
    parent = _open_parent(manager, sample_project["id"])

    close_holds_row = threading.Event()
    release_close = threading.Event()

    def close_parent() -> None:
        with temp_db.transaction() as conn:
            _close_task_in_txn(conn, parent.id, db=None, reason="completed")
            close_holds_row.set()
            release_close.wait(timeout=JOIN_TIMEOUT_SECONDS)

    def create_child() -> None:
        manager.create_task(
            sample_project["id"],
            "Child racing the close",
            parent_task_id=parent.id,
            validation_criteria=CRITERIA,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        closer = executor.submit(close_parent)
        assert close_holds_row.wait(timeout=JOIN_TIMEOUT_SECONDS)

        creator = executor.submit(create_child)
        done, _ = wait_for_futures([creator], timeout=BLOCKED_FOR_SECONDS)
        assert not done, "create did not wait on the parent row held by the close"

        release_close.set()
        closer.result(timeout=JOIN_TIMEOUT_SECONDS)
        with pytest.raises(ParentTaskClosedError):
            creator.result(timeout=JOIN_TIMEOUT_SECONDS)

    assert temp_db.fetchall("SELECT id FROM tasks WHERE parent_task_id = %s", (parent.id,)) == []


def test_close_waits_for_a_concurrent_child_insert_and_is_refused(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """A close that starts mid-create blocks on its own row, then refuses.

    Without the FOR UPDATE at the top of _close_task_in_txn the open-children
    count runs before any lock is taken, so the close misses the in-flight
    child and finishes anyway.
    """
    manager = LocalTaskManager(temp_db)
    parent = _open_parent(manager, sample_project["id"])

    create_holds_row = threading.Event()
    release_create = threading.Event()

    def create_child() -> None:
        with temp_db.transaction_immediate(
            TaskSeqAllocation(project_id=sample_project["id"])
        ) as conn:
            _create_task_in_transaction(
                temp_db,
                conn,
                project_id=sample_project["id"],
                title="Child racing the close",
                parent_task_id=parent.id,
                validation_criteria=CRITERIA,
            )
            create_holds_row.set()
            release_create.wait(timeout=JOIN_TIMEOUT_SECONDS)

    def close_parent() -> None:
        with temp_db.transaction() as conn:
            _close_task_in_txn(conn, parent.id, db=None, reason="completed")

    with ThreadPoolExecutor(max_workers=2) as executor:
        creator = executor.submit(create_child)
        assert create_holds_row.wait(timeout=JOIN_TIMEOUT_SECONDS)

        closer = executor.submit(close_parent)
        done, _ = wait_for_futures([closer], timeout=BLOCKED_FOR_SECONDS)
        assert not done, "close did not wait on its own row held by the in-flight create"

        release_create.set()
        creator.result(timeout=JOIN_TIMEOUT_SECONDS)
        with pytest.raises(TaskHasOpenChildrenError):
            closer.result(timeout=JOIN_TIMEOUT_SECONDS)

    assert manager.get_task(parent.id).closed_at is None
