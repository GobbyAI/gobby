from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from gobby.agents.task_recovery import TaskRecoveryHandler
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager


@dataclass(frozen=True)
class _Run:
    id: str
    status: str
    task_id: str | None
    child_session_id: str | None
    claimed_session_id: str | None
    provider: str = "codex"
    error: str | None = "failed"


class _RunManager:
    def get(self, run_id: str) -> _Run | None:
        return None

    def list_by_status(self, status: str, *, limit: int = 100) -> list[_Run]:
        return []


class _Classifier:
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
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    task = task_manager.create_task(sample_project["id"], "Recover task")
    task_manager.claim_task(task.id, session.id)
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn",
        ttl_seconds=30,
        run_id="run-terminal",
    )
    run = _Run(
        id="run-terminal",
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
