"""Dispatch event handlers for releasing runtime mutex leases."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

from gobby.build.dispatch_tick import schedule_dispatcher_continuation_for_task
from gobby.dispatch.mutex import RuntimeDispatchMutex
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import (
    IllegalStageTransitionError,
    TaskDispatchMutexManager,
    TaskLifecycleEventManager,
)
from gobby.storage.tasks._stage_states import StageStatesManager

logger = logging.getLogger(__name__)


def on_pipeline_completed(
    event: object,
    *,
    db: HubDatabase | None = None,
    storage: TaskDispatchMutexManager | None = None,
) -> object | None:
    """Advance the stage that launched a completed stage pipeline."""
    return _handle_stage_pipeline_terminal(event, "completed", db=db, storage=storage)


def on_pipeline_failed(
    event: object,
    *,
    db: HubDatabase | None = None,
    storage: TaskDispatchMutexManager | None = None,
) -> object | None:
    """Fail the stage that launched a failed stage pipeline."""
    return _handle_stage_pipeline_terminal(event, "failed", db=db, storage=storage)


def on_pipeline_cancelled(
    event: object,
    *,
    db: HubDatabase | None = None,
    storage: TaskDispatchMutexManager | None = None,
) -> object | None:
    """Escalate the stage that launched a cancelled stage pipeline."""
    return _handle_stage_pipeline_terminal(event, "cancelled", db=db, storage=storage)


def _handle_stage_pipeline_terminal(
    event: object,
    status: str,
    *,
    db: HubDatabase | None,
    storage: TaskDispatchMutexManager | None,
) -> object | None:
    run_id = _event_value(event, "execution_id") or _event_value(event, "run_id")
    if not run_id:
        return None
    resolved_storage = _storage_from_db(db, storage)
    if resolved_storage is None:
        return None
    mutex = resolved_storage.get_mutex_by_run_id(str(run_id))
    if mutex is None or not str(mutex.action_kind or "").startswith("stage-pipeline:"):
        return None
    if db is None:
        _release_run_mutex(str(run_id), storage=resolved_storage)
        return None
    stage_name = str(mutex.action_kind).split(":", 1)[1]
    manager = _stage_states(db)
    if manager is None:
        _release_run_mutex(str(run_id), storage=resolved_storage)
        return None
    if not mutex.lease_holder or not resolved_storage.refresh_mutex_for_run(
        mutex.task_id,
        str(run_id),
        mutex.lease_holder,
        ttl_seconds=30,
    ):
        return None
    try:
        stage = _current_stage_for_task(manager, mutex.task_id)
        if stage is None or stage.stage_name != stage_name or stage.state != "in_progress":
            return None
        if status == "completed":
            try:
                if stage.review_policy == "required":
                    updated = manager.submit_for_review(
                        mutex.task_id,
                        stage_name,
                        by_session_id=None,
                        preheld_mutex_run_id=str(run_id),
                    )
                    try:
                        schedule_dispatcher_continuation_for_task(
                            db,
                            task_id=mutex.task_id,
                            reason="stage_pipeline_review",
                        )
                    except Exception:
                        logger.warning(
                            "Failed to schedule dispatcher continuation after stage review",
                            extra={
                                "task_id": mutex.task_id,
                                "stage_name": stage_name,
                                "run_id": str(run_id),
                            },
                            exc_info=True,
                        )
                    return updated
                return manager.complete_stage(
                    mutex.task_id,
                    stage_name,
                    by_session_id=None,
                    preheld_mutex_run_id=str(run_id),
                )
            except IllegalStageTransitionError:
                return None
        if status == "cancelled":
            try:
                return manager.fail_stage(
                    mutex.task_id,
                    stage_name,
                    reason="pipeline_cancelled",
                    needs_human=True,
                    by_session_id=None,
                    preheld_mutex_run_id=str(run_id),
                )
            except IllegalStageTransitionError:
                return None
        reason = _event_value(event, "error") or _event_value(event, "reason") or "pipeline_failed"
        try:
            return manager.fail_stage(
                mutex.task_id,
                stage_name,
                reason=str(reason),
                by_session_id=None,
                preheld_mutex_run_id=str(run_id),
            )
        except IllegalStageTransitionError:
            return None
    finally:
        _release_run_mutex(str(run_id), storage=resolved_storage)


def _current_stage_for_task(manager: StageStatesManager, task_id: str) -> Any | None:
    stages = manager.list_for_task(task_id)
    pending = [stage for stage in stages if stage.state != "done"]
    return min(pending, key=lambda stage: stage.position) if pending else None


def _storage_from_db(
    db: HubDatabase | None,
    storage: TaskDispatchMutexManager | None,
) -> TaskDispatchMutexManager | None:
    if storage is not None or db is None:
        return storage
    return TaskDispatchMutexManager(db)


def _stage_states(db: HubDatabase | None) -> StageStatesManager | None:
    if db is None:
        return None
    return StageStatesManager(db, TaskLifecycleEventManager(db))


def _release_run_mutex(run_id: str, *, storage: TaskDispatchMutexManager | None) -> int:
    if storage is None:
        return 0
    return RuntimeDispatchMutex.force_release_for_run(storage, run_id)


def _event_value(event: object, key: str) -> object | None:
    if isinstance(event, Mapping):
        return cast(object | None, event.get(key))
    data = getattr(event, "data", None)
    if isinstance(data, Mapping) and key in data:
        return cast(object | None, data[key])
    return getattr(event, key, None)


__all__ = [
    "RuntimeDispatchMutex",
    "on_pipeline_cancelled",
    "on_pipeline_completed",
    "on_pipeline_failed",
]
