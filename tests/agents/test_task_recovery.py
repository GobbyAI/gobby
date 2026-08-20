from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.task_recovery import TaskRecoveryHandler
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    # Patch the cache, not the function: production modules bind get_machine_id
    # by direct import, so a function patch misses those call sites.
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@dataclass(frozen=True)
class _Run:
    id: str
    status: str
    task_id: str | None
    child_session_id: str | None
    claimed_session_id: str | None
    provider: str = "codex"
    error: str | None = "failed"
    pid: int | None = None
    terminal_id: str | None = None


class _RunManager:
    def get(self, run_id: str) -> _Run | None:
        return None

    def list_by_status(self, status: str, *, limit: int = 100) -> list[_Run]:
        return []


class _Classifier:
    def for_provider(self, provider_id: str) -> _Classifier:
        return self

    def is_provider_error(self, error_string: str | None) -> bool:
        return False

    def is_bootstrap_stall(self, error_string: str | None) -> bool:
        return False


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_failed_non_in_progress_recovery_releases_run_mutex(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    session = SessionManager(temp_db).register(
        external_id="task-recovery-owner",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    task = task_manager.create_task(
        sample_project["id"],
        "Recover task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.claim_task(task.id, session.id)
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn",
        ttl_seconds=30,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd2001",
    )
    run = _Run(
        id="dddddddd-dddd-4ddd-8ddd-dddddddd2001",
        status="failed",
        task_id=task.id,
        child_session_id=session.id,
        claimed_session_id=session.id,
    )
    handler = TaskRecoveryHandler(
        task_manager,
        _RunManager(),
        _Classifier(),
        run_db=_run_db,
    )

    recovered = await handler.recover_task_from_terminal_agent(run, outcome="failed")

    assert recovered is True
    assert task_manager.get_task(task.id).claimed_by_session_id is None
    assert mutexes.get_mutex(task.id) is None


@pytest.mark.asyncio
async def test_resolve_claimed_task_requires_child_session_ownership(
    temp_db,
    sample_project,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    session = SessionManager(temp_db).register(
        external_id="task-recovery-claimed-owner",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    task = task_manager.create_task(
        sample_project["id"],
        "Recover claimed task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.claim_task(task.id, session.id)
    handler = TaskRecoveryHandler(
        task_manager,
        _RunManager(),
        _Classifier(),
        run_db=_run_db,
    )

    # Ownership is narrowed to child_session_id (#17367): a run without a child
    # session never resolves, even when claimed_session_id matches the owner.
    childless_run = _Run(
        id="dddddddd-dddd-4ddd-8ddd-dddddddd2002",
        status="failed",
        task_id=task.id,
        child_session_id=None,
        claimed_session_id=session.id,
    )
    assert await handler.resolve_claimed_task_for_run(childless_run) is None

    owning_run = _Run(
        id="dddddddd-dddd-4ddd-8ddd-dddddddd2003",
        status="failed",
        task_id=task.id,
        child_session_id=session.id,
        claimed_session_id=None,
    )
    resolved = await handler.resolve_claimed_task_for_run(owning_run)
    assert resolved is not None
    assert resolved[0] == task.id


def test_release_task_claim_mutex_construction_type_error_falls_back() -> None:
    task_manager = MagicMock()
    task_manager.db = object()
    task_manager.release_task_claim.return_value = "released"
    handler = TaskRecoveryHandler(task_manager, _RunManager(), _Classifier())

    with patch(
        "gobby.agents.task_recovery.RuntimeDispatchMutex",
        side_effect=TypeError("old signature"),
    ):
        assert handler._release_task_claim_with_mutex("task-1") == "released"

    task_manager.release_task_claim.assert_called_once_with("task-1")


def test_release_task_claim_type_error_is_not_swallowed() -> None:
    class _Mutex:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    task_manager = MagicMock()
    task_manager.db = object()
    task_manager.release_task_claim.side_effect = TypeError("release failed")
    handler = TaskRecoveryHandler(task_manager, _RunManager(), _Classifier())

    with (
        patch("gobby.agents.task_recovery.RuntimeDispatchMutex", return_value=_Mutex()),
        pytest.raises(TypeError, match="release failed"),
    ):
        handler._release_task_claim_with_mutex("task-1")

    task_manager.release_task_claim.assert_called_once_with("task-1")
