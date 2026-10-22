"""Reopen behavior for task claims, dispatch leases, and active agent runs."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _task(temp_db: HubDatabase, sample_project: dict[str, Any]) -> tuple[LocalTaskManager, Task]:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Reopen task",
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    return manager, task


def test_reopen_blocks_active_dispatch_mutex(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager, task = _task(temp_db, sample_project)
    TaskDispatchMutexManager(temp_db).acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        ttl_seconds=300,
    )

    with pytest.raises(ValueError, match="has an active agent run or dispatch lease; stop it"):
        manager.reopen_task(task.id)


def _review_session(temp_db: HubDatabase, project_id: str, name: str) -> str:
    return (
        SessionManager(temp_db)
        .register(
            external_id=name,
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=project_id,
        )
        .id
    )


def _queue_closure(temp_db: HubDatabase, session_id: str, task_id: str) -> dict[str, str]:
    entry = {
        "closure_id": f"{task_id}:closed",
        "task_id": task_id,
        "task_ref": f"ref-{task_id}",
        "changes_summary": "Closed work.",
    }
    SessionVariableManager(temp_db).queue_memory_review(session_id, entry)
    return entry


def _pending(temp_db: HubDatabase, session_id: str) -> list[dict[str, str]]:
    pending = SessionVariableManager(temp_db).get_variables(session_id)
    return list(pending["_memory_pending_task_reviews"])


def test_withdraw_drops_only_the_reopened_task_on_every_session(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    first = _review_session(temp_db, sample_project["id"], "withdraw-first")
    second = _review_session(temp_db, sample_project["id"], "withdraw-second")
    kept = _queue_closure(temp_db, first, "task-kept")
    _queue_closure(temp_db, first, "task-reopened")
    _queue_closure(temp_db, second, "task-reopened")
    state = SessionVariableManager(temp_db)
    state.merge_variables(
        first, {"_memory_task_review_records": [{"closure_id": "task-kept:closed"}]}
    )

    state.withdraw_memory_reviews("task-reopened")

    assert _pending(temp_db, first) == [kept]
    assert _pending(temp_db, second) == []
    # A reviewed remainder needs no delivery, as review_task_memories settles it.
    assert state.get_variables(first)["_memory_review_stop_delivered"] is True


def test_withdraw_inside_a_transaction_waits_for_its_commit(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    session_id = _review_session(temp_db, sample_project["id"], "withdraw-deferred")
    entry = _queue_closure(temp_db, session_id, "task-deferred")
    state = SessionVariableManager(temp_db)

    with pytest.raises(RuntimeError, match="rolled back"):
        with temp_db.transaction():
            state.withdraw_memory_reviews("task-deferred")
            raise RuntimeError("rolled back")
    after_rollback = _pending(temp_db, session_id)
    with temp_db.transaction():
        state.withdraw_memory_reviews("task-deferred")
        inside_commit = _pending(temp_db, session_id)

    assert after_rollback == [entry]
    assert inside_commit == [entry]
    assert _pending(temp_db, session_id) == []


def test_reopen_ignores_expired_dispatch_mutex(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager, task = _task(temp_db, sample_project)
    manager.close_task(task.id)
    expired = datetime.now(UTC) - timedelta(seconds=60)
    TaskDispatchMutexManager(temp_db).acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        ttl_seconds=1,
        now=expired,
    )

    reopened = manager.reopen_task(task.id)

    assert reopened.closed_at is None
    assert reopened.claimed_by_session_id is None


def test_reopen_blocks_active_agent_run(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager, task = _task(temp_db, sample_project)
    parent = SessionManager(temp_db).register(
        external_id="reopen-parent",
        machine_id="21000000-0000-4000-8000-000000000003",
        source="codex",
        project_id=sample_project["id"],
    )
    LocalAgentRunManager(temp_db).create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="work",
        task_id=task.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddddd20",
    )

    with pytest.raises(ValueError, match="has an active agent run or dispatch lease; stop it"):
        manager.reopen_task(task.id)
