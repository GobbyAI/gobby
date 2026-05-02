"""Dispatch event handlers for releasing runtime mutex leases."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from gobby.dispatch.actions import Action, EscalateAction
from gobby.dispatch.mutex import RuntimeDispatchMutex
from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks import TaskDispatchMutexManager, TaskLifecycleEventManager
from gobby.storage.tasks._stage_states import StageStatesManager


def on_agent_terminal(event: object, storage: TaskDispatchMutexManager | None = None) -> int:
    """Release the dispatch mutex for a terminal agent run."""
    return _release_event_mutex(event, storage=_event_storage(event, storage))


def on_agent_crashed(event: object, storage: TaskDispatchMutexManager | None = None) -> int:
    """Release the dispatch mutex for a crashed agent run."""
    return _release_event_mutex(event, storage=_event_storage(event, storage))


def on_task_reopened(event: object, storage: TaskDispatchMutexManager | None = None) -> int:
    """Release the dispatch mutex when a task is reopened."""
    return _release_event_mutex(event, storage=_event_storage(event, storage))


def on_agent_end_normal(event: object, storage: TaskDispatchMutexManager | None = None) -> int:
    """Release the dispatch mutex for a clean end-agent path."""
    return _release_event_mutex(event, storage=_event_storage(event, storage))


def on_claim_released(event: object, storage: TaskDispatchMutexManager | None = None) -> int:
    """Release the dispatch mutex when an agent claim is released."""
    return _release_event_mutex(event, storage=_event_storage(event, storage))


def on_expansion_run_completed(
    task_id: str,
    expansion_run_id: str,
    *,
    apply_created_children: bool = True,
    db: DatabaseProtocol | None = None,
    storage: TaskDispatchMutexManager | None = None,
) -> object | None:
    """Advance successful applied expansion runs and release their mutex."""
    result: object | None = None
    resolved_storage = _storage_from_db(db, storage)
    try:
        if apply_created_children:
            result = _complete_stage(
                task_id,
                stage_name="expansion",
                db=db,
            )
        return result
    finally:
        _release_run_mutex(expansion_run_id, storage=resolved_storage)


def on_expansion_run_failed(
    task_id: str,
    expansion_run_id: str,
    *,
    reason: str,
    expansion_attempts: int | None = None,
    max_expansion_attempts: int | None = None,
    unattended: bool = False,
    db: DatabaseProtocol | None = None,
    storage: TaskDispatchMutexManager | None = None,
) -> Action | object | None:
    """Handle failed expansion runs, retrying until the attempt cap is exhausted."""
    resolved_storage = _storage_from_db(db, storage)
    try:
        attempts = expansion_attempts or 0
        if max_expansion_attempts is not None and attempts >= max_expansion_attempts:
            if unattended:
                return _complete_stage(
                    task_id,
                    stage_name="expansion",
                    db=db,
                )
            return EscalateAction(task_id=task_id, reason=f"expansion_run_failed:{reason}")

        return _fail_stage(
            task_id,
            stage_name="expansion",
            reason=f"expansion_run_failed:{reason}",
            db=db,
        )
    finally:
        _release_run_mutex(expansion_run_id, storage=resolved_storage)


def on_expansion_run_cancelled(
    task_id: str,
    expansion_run_id: str,
    *,
    storage: TaskDispatchMutexManager | None = None,
) -> int:
    """Release the mutex for operator-cancelled expansion runs."""
    _ = task_id
    return _release_run_mutex(expansion_run_id, storage=storage)


def _release_event_mutex(event: object, *, storage: TaskDispatchMutexManager | None) -> int:
    run_id = _event_value(event, "run_id")
    if run_id:
        return _release_run_mutex(str(run_id), storage=storage)

    task_id = _event_value(event, "task_id")
    if not task_id or storage is None:
        return 0
    return int(RuntimeDispatchMutex.force_release_for_task(storage, str(task_id)))


def _event_storage(
    event: object, storage: TaskDispatchMutexManager | None
) -> TaskDispatchMutexManager | None:
    if storage is not None:
        return storage
    for key in ("dispatch_mutex_storage", "task_dispatch_mutex_manager", "mutex_storage"):
        candidate = _event_value(event, key)
        if isinstance(candidate, TaskDispatchMutexManager):
            return candidate
    db = _event_value(event, "db")
    if db is None:
        return None
    return TaskDispatchMutexManager(cast(DatabaseProtocol, db))


def _storage_from_db(
    db: DatabaseProtocol | None,
    storage: TaskDispatchMutexManager | None,
) -> TaskDispatchMutexManager | None:
    if storage is not None or db is None:
        return storage
    return TaskDispatchMutexManager(db)


def _stage_states(db: DatabaseProtocol | None) -> StageStatesManager | None:
    if db is None:
        return None
    return StageStatesManager(db, TaskLifecycleEventManager(db))


def _complete_stage(
    task_id: str,
    *,
    stage_name: str,
    db: DatabaseProtocol | None,
) -> object | None:
    manager = _stage_states(db)
    if manager is None:
        return None
    return manager.complete_stage(task_id, stage_name, by_session_id=None)


def _fail_stage(
    task_id: str,
    *,
    stage_name: str,
    reason: str,
    db: DatabaseProtocol | None,
) -> object | None:
    manager = _stage_states(db)
    if manager is None:
        return None
    return manager.fail_stage(task_id, stage_name, reason=reason, by_session_id=None)


def _release_run_mutex(run_id: str, *, storage: TaskDispatchMutexManager | None) -> int:
    if storage is not None:
        return RuntimeDispatchMutex.force_release_for_run(storage, run_id)

    try:
        release_for_run = cast(Any, RuntimeDispatchMutex.force_release_for_run)
        return int(release_for_run(run_id))
    except TypeError:
        return 0


def _event_value(event: object, key: str) -> object | None:
    if isinstance(event, Mapping):
        return cast(object | None, event.get(key))
    data = getattr(event, "data", None)
    if isinstance(data, Mapping) and key in data:
        return cast(object | None, data[key])
    return getattr(event, key, None)


__all__ = [
    "RuntimeDispatchMutex",
    "on_agent_crashed",
    "on_agent_end_normal",
    "on_agent_terminal",
    "on_claim_released",
    "on_expansion_run_cancelled",
    "on_expansion_run_completed",
    "on_expansion_run_failed",
    "on_task_reopened",
]
