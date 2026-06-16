"""Spawn action execution and cleanup helpers for dispatcher heartbeat."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.mutex import RuntimeDispatchMutex, RuntimeDispatchMutexError
from gobby.dispatch.spawn import (
    MAX_DISPATCH_SPAWN_ATTEMPTS,
    DispatchSpawnFailed,
    DispatchSpawnUnavailable,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._stage_types import IllegalStageTransitionError

logger = logging.getLogger(__name__)

RunDb = Callable[..., Awaitable[Any]]
AsyncCallback = Callable[..., Awaitable[Any]]
FieldGetter = Callable[[object | None, str, object | None], object | None]
StageNameGetter = Callable[[object | None], str | None]
StageStatesManagerFactory = Callable[..., Any]


async def execute_spawn_action(
    action: SpawnAgentAction,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None,
    services: object | None,
    run_db: RunDb,
    spawn_agent: Callable[..., object],
    handle_spawn_failure: AsyncCallback,
    cleanup_unattached_spawned_run: AsyncCallback,
    try_resume_daemon_stop_run: AsyncCallback,
) -> str | None:
    resume_result = await try_resume_daemon_stop_run(
        action,
        mutex=mutex,
        db=db,
        context=context,
        services=services,
    )
    if resume_result.handled:
        return cast(str | None, resume_result.run_id)
    try:
        raw_run_id: object = spawn_agent(
            action,
            db=db,
            context=context,
            services=services,
            mutex=mutex,
        )
        if inspect.isawaitable(raw_run_id):
            raw_run_id = await cast(Awaitable[str | None], raw_run_id)
    except (DispatchSpawnFailed, DispatchSpawnUnavailable):
        raise
    except BaseException:
        if mutex.run_id is None:
            await run_db(mutex.release)
        raise
    if raw_run_id:
        run_id = str(raw_run_id)
        try:
            await run_db(mutex.attach, run_id)
        except RuntimeDispatchMutexError as exc:
            await cleanup_unattached_spawned_run(run_id, db=db, error=str(exc))
            await handle_spawn_failure(
                action,
                mutex=mutex,
                db=db,
                context=context,
                error=f"dispatch_mutex_attach_failed:{exc}",
            )
            return None
        return run_id
    await handle_spawn_failure(action, mutex=mutex, db=db, context=context, error="missing run_id")
    return None


async def cleanup_unattached_spawned_run(
    run_id: str,
    *,
    db: HubDatabase,
    error: str,
) -> None:
    run_storage = LocalAgentRunManager(db)
    run = run_storage.get(run_id)
    if run is None or run.status not in ("pending", "running"):
        return

    try:
        from gobby.agents.kill import kill_agent

        await kill_agent(run, db, close_terminal=True)
    except Exception:
        logger.warning(
            "Failed to kill unattached spawned agent run %s after mutex attach failure",
            run_id,
            exc_info=True,
        )

    failed = run_storage.fail(run_id, error=f"dispatch mutex attach failed: {error}")
    if failed is None:
        logger.debug(
            "Unattached spawned agent run %s was already terminal during attach-failure cleanup",
            run_id,
        )


async def handle_spawn_failure(
    action: SpawnAgentAction,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None,
    error: str,
    cited_subtasks: Sequence[str] | None = None,
    append_audit_marker: AsyncCallback,
    get_task: Callable[[HubDatabase, str], object],
    update_task: Callable[..., object],
    escalate_task: Callable[..., bool],
    field: FieldGetter,
    stage_name: StageNameGetter,
    stage_states_manager: StageStatesManagerFactory,
) -> None:
    try:
        await append_audit_marker(db, action.task_id, "Dispatch spawn failed", error)
        cited_subtask_ids = tuple(cited_subtasks or ())
        failure_count = 0
        if not cited_subtask_ids:
            task = get_task(db, action.task_id)
            failure_count = int(getattr(task, "dispatch_failure_count", 0) or 0) + 1
            update_task(db, action.task_id, dispatch_failure_count=failure_count)
        stage = field(context, "current_stage", None)
        current_stage_name = stage_name(stage)
        if current_stage_name and field(stage, "state", None) == "in_progress":
            failure_reason = (
                f"dispatch_spawn_failed:{error}" if cited_subtask_ids else "dispatch_spawn_failed"
            )
            try:
                mutex.release()
                stage_states_manager(
                    db=db,
                    services=getattr(context, "services", None),
                ).fail_stage(
                    action.task_id,
                    current_stage_name,
                    reason=failure_reason,
                    by_session_id="dispatcher",
                    cited_subtasks=cited_subtask_ids,
                )
            except IllegalStageTransitionError:
                fresh_stage = stage_states_manager(
                    db=db,
                    services=getattr(context, "services", None),
                ).get(action.task_id, current_stage_name)
                if fresh_stage is None or fresh_stage.state != "ready":
                    raise
                logger.info(
                    "Dispatch spawn failure rollback already applied: task_id=%s stage_name=%s",
                    action.task_id,
                    current_stage_name,
                )
            except Exception:
                logger.warning(
                    "Failed to roll back stage after dispatch spawn failure: "
                    "task_id=%s stage_name=%s by_session_id=%s",
                    action.task_id,
                    current_stage_name,
                    "dispatcher",
                    exc_info=True,
                )
        if not cited_subtask_ids and failure_count >= MAX_DISPATCH_SPAWN_ATTEMPTS:
            escalate_task(
                db=db,
                task_id=action.task_id,
                reason=f"dispatch_spawn_max_attempts:{error}",
            )
    finally:
        mutex.release()
