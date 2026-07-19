"""Stage pipeline dispatch helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, cast

import psycopg

from gobby.dispatch.actions import StartPipelineAction
from gobby.dispatch.mutex import RuntimeDispatchMutex, RuntimeDispatchMutexError
from gobby.storage.hub.protocol import DispatchMutexRow, HubDatabase
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine

logger = logging.getLogger(__name__)

_PIPELINE_ATTACH_DATABASE_ERRORS = (
    psycopg.IntegrityError,
    psycopg.OperationalError,
    psycopg.Error,
)

FieldGetter = Callable[[object | None, str, object | None], object | None]
EscalatePipelineDispatch = Callable[
    [StartPipelineAction, RuntimeDispatchMutex, HubDatabase, str], dict[str, object]
]
RetryNeutralPipelineDispatch = Callable[
    [StartPipelineAction, RuntimeDispatchMutex, HubDatabase, str], dict[str, object]
]
RenderDispatchInputs = Callable[[StartPipelineAction, object | None, object | None], dict[str, Any]]
CreatePipelineExecution = Callable[..., str]
ExecutePipelineBackground = Callable[..., Coroutine[Any, Any, Any]]
RegisterBackgroundTask = Callable[[str, asyncio.Task[Any]], object]
EscalateTask = Callable[..., bool]
StageStatesManagerFactory = Callable[..., Any]

# Consecutive-retry ceiling for the internal stage-pipeline mutex race. Each
# retry-neutral restore increments retry_neutral_failure_count on the stage row;
# a successful attach resets it to 0 (see start_pipeline_action). If a pipeline
# stage cannot attach after this many consecutive retry-neutral restores the
# task escalates instead of spinning on the assumption that the mutex lease will
# eventually expire.
MAX_PIPELINE_RETRY_NEUTRAL_RESTORES = 3


async def start_pipeline_action(
    action: StartPipelineAction,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None,
    services: object | None,
    field: FieldGetter,
    escalate_pipeline_dispatch: EscalatePipelineDispatch,
    retry_neutral_pipeline_dispatch: RetryNeutralPipelineDispatch,
    render_dispatch_inputs: RenderDispatchInputs,
    create_stage_pipeline_execution: CreatePipelineExecution,
    execute_pipeline_background: ExecutePipelineBackground,
    register_background_task: RegisterBackgroundTask,
) -> dict[str, object]:
    executor = getattr(services, "pipeline_executor", None)
    loader = getattr(services, "workflow_loader", None) or getattr(executor, "loader", None)
    if executor is None:
        return escalate_pipeline_dispatch(action, mutex, db, "pipeline_executor_missing")
    if loader is None:
        return escalate_pipeline_dispatch(action, mutex, db, "pipeline_loader_missing")

    project_id = str(field(context, "project_id", ""))
    try:
        pipeline = await loader.load_pipeline(action.pipeline_name, project_id)
    except ValueError as exc:
        return escalate_pipeline_dispatch(action, mutex, db, f"pipeline_invalid:{exc}")
    if pipeline is None:
        return escalate_pipeline_dispatch(
            action, mutex, db, f"pipeline_missing:{action.pipeline_name}"
        )
    if not getattr(pipeline, "enabled", True):
        return escalate_pipeline_dispatch(
            action, mutex, db, f"pipeline_disabled:{action.pipeline_name}"
        )
    if getattr(pipeline, "deprecated", False):
        return escalate_pipeline_dispatch(
            action, mutex, db, f"pipeline_deprecated:{action.pipeline_name}"
        )

    try:
        inputs = render_dispatch_inputs(action, context, services)
    except Exception as exc:
        return escalate_pipeline_dispatch(action, mutex, db, f"pipeline_render_failed:{exc}")

    try:
        execution_id = create_stage_pipeline_execution(
            action,
            pipeline=pipeline,
            inputs=inputs,
            mutex=mutex,
            db=db,
            services=services,
        )
    except _PIPELINE_ATTACH_DATABASE_ERRORS as exc:
        return escalate_pipeline_dispatch(
            action, mutex, db, f"pipeline_attach_failed:database:{exc}"
        )
    except RuntimeDispatchMutexError as exc:
        return retry_neutral_pipeline_dispatch(action, mutex, db, str(exc))
    except RuntimeError as exc:
        return escalate_pipeline_dispatch(action, mutex, db, f"pipeline_attach_failed:{exc}")
    except Exception as exc:
        logger.exception(
            "Unexpected pipeline attach failure for task %s pipeline %s",
            action.task_id,
            action.pipeline_name,
        )
        return escalate_pipeline_dispatch(
            action, mutex, db, f"pipeline_attach_failed:unexpected:{exc}"
        )
    reset_stage_pipeline_retry_neutral(db, action.task_id, action.stage_name)
    coro = execute_pipeline_background(
        executor,
        pipeline,
        inputs,
        project_id,
        execution_id,
        action.pipeline_name,
        session_id=getattr(services, "triggering_session_id", None),
    )
    task_name = f"stage-pipeline-{action.pipeline_name}-{execution_id[:8]}"
    main_loop = getattr(services, "main_loop", None)
    current_loop = asyncio.get_running_loop()
    if main_loop is not None and main_loop is not current_loop and not main_loop.is_closed():
        # A tick may run on a short-lived loop (the HTTP build route drives the
        # service via asyncio.run in a worker thread); a task created there is
        # cancelled with that loop and the execution row strands at 'pending'
        # until the heartbeat fails it, so the coroutine must live on the
        # daemon's main loop instead.
        def _spawn_on_main_loop() -> None:
            spawned: asyncio.Task[Any] = asyncio.create_task(coro, name=task_name)
            register_background_task(execution_id, spawned)

        main_loop.call_soon_threadsafe(_spawn_on_main_loop)
        return {"success": True, "execution_id": execution_id, "status": "running"}
    task: asyncio.Task[Any] = asyncio.create_task(coro, name=task_name)
    register_background_task(execution_id, task)
    return {"success": True, "execution_id": execution_id, "status": "running"}


def escalate_pipeline_dispatch(
    action: StartPipelineAction,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    reason: str,
    *,
    escalate_task: EscalateTask,
) -> dict[str, object]:
    try:
        escalate_task(db=db, task_id=action.task_id, reason=f"stage_pipeline_dispatch:{reason}")
    finally:
        mutex.release()
    return {"success": False, "error": reason}


def retry_neutral_pipeline_dispatch(
    action: StartPipelineAction,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    reason: str,
    *,
    restore_stage_pipeline_retry: Callable[..., int],
    escalate_task: EscalateTask,
    ceiling: int = MAX_PIPELINE_RETRY_NEUTRAL_RESTORES,
) -> dict[str, object]:
    mutex.release()
    failure_count = restore_stage_pipeline_retry(
        db, action.task_id, action.stage_name, reason=reason
    )
    if failure_count >= ceiling:
        escalate_task(
            db=db,
            task_id=action.task_id,
            reason=f"stage_pipeline_dispatch_retry_neutral:max:{reason}",
        )
        return {"success": False, "error": reason, "retry_neutral": False, "escalated": True}
    return {"success": False, "error": reason, "retry_neutral": True}


def restore_stage_pipeline_retry(
    db: HubDatabase,
    task_id: str,
    stage_name: str,
    *,
    reason: str,
    stage_states_manager: StageStatesManagerFactory,
) -> int:
    """Restore a stage after a retry-neutral pipeline attach failure.

    Returns the new persistent ``retry_neutral_failure_count`` for the stage
    (0 when no restore happened). work_attempt_count is decremented so the retry
    is attempt-neutral, but the retry-neutral failure counter is incremented so a
    caller can escalate once consecutive restores cross a ceiling.
    """
    stage = stage_states_manager(db=db, services=None).get(task_id, stage_name)
    if stage is None or stage.state != "in_progress":
        return 0
    now = datetime.now(UTC).isoformat()
    with db.transaction() as conn:
        row = conn.execute(
            """
            UPDATE task_stage_states
               SET state = 'ready',
                   entered_at = NULL,
                   entered_by_session_id = NULL,
                   entered_by_actor = NULL,
                   artifact_refs = NULL,
                   notes = NULL,
                   work_attempt_count = CASE
                       WHEN work_attempt_count > 0 THEN work_attempt_count - 1
                       ELSE 0
                   END,
                   retry_neutral_failure_count = retry_neutral_failure_count + 1,
                   updated_at = %s
             WHERE task_id = %s
               AND stage_name = %s
               AND state = 'in_progress'
         RETURNING retry_neutral_failure_count
            """,
            (now, task_id, stage_name),
        ).fetchone()
        failure_count = int(row["retry_neutral_failure_count"]) if row is not None else 0
        restored = row is not None
    if restored:
        TaskLifecycleEventManager(db).record_lifecycle_event(
            task_id,
            f"{stage_name}:in_progress",
            f"{stage_name}:ready",
            f"stage_pipeline_dispatch_retry_neutral:{reason}",
            by_actor="dispatcher",
        )
    return failure_count


def reset_stage_pipeline_retry_neutral(db: HubDatabase, task_id: str, stage_name: str) -> None:
    """Clear the retry-neutral failure counter after a successful pipeline attach.

    A successful attach means the mutex contention that drove prior restores has
    cleared, so the consecutive-restore ceiling resets. Only writes when the
    counter is non-zero to avoid churn on the common path.
    """
    now = datetime.now(UTC).isoformat()
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE task_stage_states
               SET retry_neutral_failure_count = 0,
                   updated_at = %s
             WHERE task_id = %s
               AND stage_name = %s
               AND retry_neutral_failure_count <> 0
            """,
            (now, task_id, stage_name),
        )


def render_dispatch_inputs(
    action: StartPipelineAction,
    context: object | None,
    services: object | None,
    *,
    field: FieldGetter,
) -> dict[str, Any]:
    render_context = pipeline_render_context(action, context, services, field=field)
    renderer = StepRenderer(TemplateEngine())
    return renderer.render_mcp_arguments(dict(action.dispatch_inputs or {}), render_context)


def pipeline_render_context(
    action: StartPipelineAction,
    context: object | None,
    services: object | None,
    *,
    field: FieldGetter,
) -> dict[str, Any]:
    task = field(context, "task", None)
    artifacts = field(context, "artifacts", {})
    children = field(context, "children", [])
    stage_state = field(context, "current_stage", None)
    project_id = field(task, "project_id", field(context, "project_id", None))
    return {
        "task": task,
        "stage": stage_state,
        "artifacts": artifacts,
        "children": children,
        "task_id": action.task_id,
        "task_ref": action.task_ref,
        "stage_name": action.stage_name,
        "stage_state": stage_state,
        "project_id": project_id,
        "session_id": getattr(services, "triggering_session_id", None),
    }


def create_stage_pipeline_execution(
    action: StartPipelineAction,
    *,
    pipeline: object,
    inputs: dict[str, Any],
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    services: object | None,
) -> str:
    execution_id = str(uuid.uuid4())
    session_id = getattr(services, "triggering_session_id", None)
    try:
        definition_json = cast(Any, pipeline).model_dump_json()
    except Exception as exc:
        logger.warning(
            "Failed to serialize pipeline definition for %s: %s",
            action.pipeline_name,
            exc,
            exc_info=True,
        )
        definition_json = json.dumps(
            {"name": action.pipeline_name, "error": "serialization failed"}
        )
    with db.transaction_immediate(DispatchMutexRow(task_id=action.task_id)) as conn:
        cursor = conn.execute(
            """
            INSERT INTO pipeline_executions (
                id, pipeline_name, project_id, status, inputs_json, session_id,
                definition_json, created_at, updated_at
            )
            SELECT %s, %s, project_id, 'pending', %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
              FROM tasks
             WHERE id = %s
            """,
            (
                execution_id,
                action.pipeline_name,
                json.dumps(inputs),
                session_id,
                definition_json,
                action.task_id,
            ),
        )
        if cursor.rowcount < 1:
            raise RuntimeError(f"task missing before attaching {execution_id}")
        cursor = conn.execute(
            """
            UPDATE task_dispatch_mutex
               SET run_id = %s,
                   action_kind = %s,
                   updated_at = CURRENT_TIMESTAMP
             WHERE task_id = %s
            """,
            (execution_id, f"stage-pipeline:{action.stage_name}", action.task_id),
        )
        if cursor.rowcount < 1:
            raise RuntimeError(f"dispatch mutex missing before attaching {execution_id}")
    mutex.mark_attached_run_id(execution_id)
    return execution_id
