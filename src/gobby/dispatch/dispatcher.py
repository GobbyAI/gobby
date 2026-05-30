"""Heartbeat scanner for task lifecycle dispatch."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg

from gobby.dispatch import rules as dispatch_rules
from gobby.dispatch.actions import (
    Action,
    AdvanceStageAction,
    AppendAuditMarkerAction,
    CreateIsolationAction,
    EscalateAction,
    MergeWorkspaceAction,
    SpawnAgentAction,
    StartPipelineAction,
    StartStageAction,
)
from gobby.dispatch.audit import append_audit_marker
from gobby.dispatch.context import _field, build_context, reload_candidate
from gobby.dispatch.daemon_resume import try_resume_daemon_stop_run
from gobby.dispatch.mutex import (
    DispatchCandidateChangedError,
    DispatchMutexUnavailableError,
    RuntimeDispatchMutex,
    RuntimeDispatchMutexError,
    RuntimeStageSnapshotState,
)
from gobby.dispatch.spawn import (
    MAX_DISPATCH_SPAWN_ATTEMPTS,
    DispatchSpawnFailed,
    DispatchSpawnUnavailable,
    spawn_agent,
)
from gobby.dispatch.workspace_merge import execute_merge_workspace
from gobby.dispatch.write_set_guard import DispatchWriteSetGuard, WriteSetOverlap
from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
    _execute_pipeline_background,
    _register_background_task,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._ancestor_gate import find_child_development_ancestor_gate
from gobby.storage.tasks._artifacts import TaskArtifacts
from gobby.storage.tasks._artifacts import (
    set_artifacts_atomic as _set_artifacts_atomic,
)
from gobby.storage.tasks._automation import (
    list_automation_candidates,
    sweep_stale_claims,
)
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.tasks._stage_types import IllegalStageTransitionError
from gobby.storage.tasks._transitions import escalate_task as _escalate_task
from gobby.storage.tasks._updates import update_task
from gobby.utils.id import generate_prefixed_id
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine

logger = logging.getLogger(__name__)

MAX_ACTIVE_AGENTS = 10
DISPATCH_HOLDER = "dispatcher"
DISPATCH_TTL_SECONDS = 600
ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS = 30
_PIPELINE_ATTACH_DATABASE_ERRORS = (
    psycopg.IntegrityError,
    psycopg.OperationalError,
    psycopg.Error,
)


@dataclass(frozen=True)
class HeartbeatResult:
    scanned: int = 0
    executed: int = 0
    skipped: int = 0
    cap_reached: bool = False
    reason: str | None = None


def _skipped(result: HeartbeatResult) -> HeartbeatResult:
    return HeartbeatResult(
        result.scanned,
        result.executed,
        result.skipped + 1,
        result.cap_reached,
        result.reason,
    )


def _cap_reached(result: HeartbeatResult) -> HeartbeatResult:
    return HeartbeatResult(
        scanned=result.scanned,
        executed=result.executed,
        skipped=result.skipped,
        cap_reached=True,
        reason=result.reason,
    )


def _action_cap_reached(result: HeartbeatResult, max_actions: int | None) -> bool:
    return max_actions is not None and result.executed >= max_actions


def _unavailable(result: HeartbeatResult, reason: str) -> HeartbeatResult:
    return HeartbeatResult(result.scanned, result.executed, result.skipped + 1, False, reason)


async def run_heartbeat(
    *,
    db: HubDatabase | None = None,
    project_id: str | None = None,
    startup: bool = False,
    max_active_agents: int | None = None,
    holder: str = DISPATCH_HOLDER,
    ttl_seconds: int = DISPATCH_TTL_SECONDS,
    services: object | None = None,
    max_actions: int | None = None,
) -> HeartbeatResult:
    """Scan automation candidates, acquire per-task leases, and execute first-match actions."""
    from gobby.agents.readiness import spawn_readiness_blocker

    readiness_reason = spawn_readiness_blocker(services)
    if readiness_reason is not None:
        logger.info("Dispatcher heartbeat skipped: %s", readiness_reason)
        return _unavailable(HeartbeatResult(), readiness_reason)

    if db is None:
        from gobby.storage.hub.runtime import open_runtime_hub_database

        resolved_db = open_runtime_hub_database(apply_migrations=False)
    else:
        resolved_db = db
    mutex_storage = TaskDispatchMutexManager(resolved_db)
    if startup:
        sweep_expired_leases(mutex_storage)
    orphan_mutexes = sweep_orphan_no_run_dispatch_mutexes(
        mutex_storage,
        resolved_db,
        project_id=project_id,
    )
    if orphan_mutexes:
        logger.info("Dispatcher cleared %d orphan no-run mutex(es)", orphan_mutexes)
    reclaimed = sweep_stale_claims(resolved_db, project_id=project_id)
    if reclaimed:
        logger.info("Dispatcher reclaimed %d task(s) from dead sessions", reclaimed)

    cap = MAX_ACTIVE_AGENTS if max_active_agents is None else max_active_agents
    candidates = list_automation_candidates(resolved_db, project_id=project_id)
    write_set_guard = DispatchWriteSetGuard.load(resolved_db, project_id=project_id)
    result = HeartbeatResult(scanned=len(candidates))

    for candidate in candidates:
        if _action_cap_reached(result, max_actions):
            return _cap_reached(result)
        if count_active_agents(resolved_db, project_id=project_id) >= cap:
            return _cap_reached(result)

        snapshot_candidate = _candidate_for_stage_snapshot(
            candidate,
            db=resolved_db,
            project_id=project_id,
        )
        if snapshot_candidate is None:
            result = _skipped(result)
            continue
        stage_name, stage_state, stage_updated_at = _candidate_stage_snapshot(snapshot_candidate)

        mutex = RuntimeDispatchMutex(
            mutex_storage,
            task_id=candidate.id,
            holder=holder,
            action_kind="heartbeat",
            ttl_seconds=ttl_seconds,
            expected_stage_name=stage_name,
            expected_stage_state=stage_state,
            expected_stage_updated_at=stage_updated_at,
            candidate_loader=lambda task_id: reload_candidate(
                task_id,
                db=resolved_db,
                project_id=project_id,
            ),
        )
        try:
            mutex.__enter__()
        except (DispatchMutexUnavailableError, DispatchCandidateChangedError):
            result = _skipped(result)
            continue

        try:
            current = reload_candidate(candidate.id, db=resolved_db, project_id=project_id)
            if current is None or not _candidate_matches_mutex_snapshot(mutex, current):
                result = _release_and_skip(mutex, result)
                continue

            context = build_context(resolved_db, current, services=services)
            if find_child_development_ancestor_gate(
                resolved_db,
                current,
                current_stage=getattr(context, "current_stage", None),
            ):
                result = _release_and_skip(mutex, result)
                continue
            action = dispatch_rules.evaluate(current, context, _rules())
            if action is None:
                result = _release_and_skip(mutex, result)
                continue
            if write_set_guard.action_reserves_write_set(action, current):
                overlap = write_set_guard.conflict_for(action.task_id)
                if overlap is not None:
                    _log_write_set_overlap(overlap)
                    result = _release_and_skip(mutex, result)
                    continue

            action_result = await _execute_action(
                action,
                mutex=mutex,
                db=resolved_db,
                context=context,
                services=services,
            )
            if action_result is not None and write_set_guard.action_reserves_write_set(
                action, current
            ):
                write_set_guard.reserve(action.task_id)
            result = HeartbeatResult(result.scanned, result.executed + 1, result.skipped)
        except (TypeError, AttributeError, psycopg.Error):
            mutex.release()
            raise
        except DispatchSpawnUnavailable as exc:
            mutex.release()
            logger.info("Dispatcher heartbeat unavailable: %s", exc)
            return _unavailable(result, str(exc))
        except Exception as exc:
            logger.exception("Dispatcher heartbeat candidate failed: task_id=%s", candidate.id)
            try:
                append_audit_marker(
                    resolved_db,
                    candidate.id,
                    "Dispatch failed",
                    str(exc),
                )
            except Exception:
                logger.debug("Failed to append dispatch failure audit marker", exc_info=True)
            mutex.release()
            result = _skipped(result)
            continue

    return result


def _log_write_set_overlap(overlap: WriteSetOverlap) -> None:
    logger.info(
        "Dispatcher skipped overlapping write-set task",
        extra={
            "task_id": overlap.task_id,
            "blocking_task_ids": overlap.blocking_task_ids,
            "file_paths": overlap.file_paths[:10],
            "file_count": len(overlap.file_paths),
        },
    )


def _candidate_for_stage_snapshot(
    candidate: Task,
    *,
    db: HubDatabase,
    project_id: str | None,
) -> Task | None:
    if _candidate_current_stage(candidate) is not None:
        return candidate
    return reload_candidate(candidate.id, db=db, project_id=project_id)


def _candidate_matches_mutex_snapshot(
    mutex: RuntimeDispatchMutex,
    candidate: Task,
) -> bool:
    return mutex.candidate_stage_snapshot_matches(*_candidate_stage_snapshot(candidate))


def _release_and_skip(mutex: RuntimeDispatchMutex, result: HeartbeatResult) -> HeartbeatResult:
    mutex.release()
    return _skipped(result)


def sweep_orphan_no_run_dispatch_mutexes(
    mutex_storage: TaskDispatchMutexManager,
    db: HubDatabase,
    *,
    project_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Release dispatcher leases that never attached a run and aged past the grace window."""
    resolved_now = now or datetime.now(UTC)
    project_join = ""
    project_filter = ""
    params: list[object] = [DISPATCH_HOLDER]
    if project_id is not None:
        project_join = "JOIN tasks t ON t.id = mutex.task_id"
        project_filter = "AND t.project_id = %s"
        params.append(project_id)
    rows = db.fetchall(
        f"""
        SELECT mutex.task_id, mutex.lease_until, mutex.updated_at
          FROM task_dispatch_mutex mutex
          {project_join}
         WHERE mutex.lease_holder = %s
           AND mutex.run_id IS NULL
           {project_filter}
        """,  # nosec B608 # project join/filter are fixed SQL fragments selected above.
        tuple(params),
    )
    cleared = 0
    for row in rows:
        lease_until = _parse_mutex_timestamp(row["lease_until"])
        if lease_until is not None:
            if lease_until >= resolved_now:
                continue
            should_release = True
        else:
            should_release = False

        updated_at = _parse_mutex_timestamp(row["updated_at"])
        if updated_at is None and not should_release:
            continue
        if (
            not should_release
            and updated_at is not None
            and resolved_now - updated_at < timedelta(seconds=ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS)
        ):
            continue
        if _release_orphan_no_run_mutex(
            mutex_storage,
            task_id=str(row["task_id"]),
            updated_at=str(row["updated_at"]),
        ):
            cleared += 1
    return cleared


def _release_orphan_no_run_mutex(
    mutex_storage: TaskDispatchMutexManager,
    *,
    task_id: str,
    updated_at: str,
) -> bool:
    with mutex_storage.db.transaction() as conn:
        cursor = conn.execute(
            """
            DELETE FROM task_dispatch_mutex
             WHERE task_id = %s
               AND lease_holder = %s
               AND run_id IS NULL
               AND updated_at = %s
            """,
            (task_id, DISPATCH_HOLDER, updated_at),
        )
        return cursor.rowcount > 0


def _parse_mutex_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _rules() -> list[Any]:
    return list(getattr(dispatch_rules, "RULES", dispatch_rules.BASE_RULES))


async def _execute_action(
    action: Action,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object,
    services: object | None,
) -> object | None:
    result = execute_action(
        action,
        mutex=mutex,
        db=db,
        context=context,
        services=services,
    )
    if inspect.isawaitable(result):
        return await cast(Awaitable[object | None], result)
    return result


async def _execute_spawn_action(
    action: SpawnAgentAction,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None,
    services: object | None,
) -> str | None:
    resume_result = await try_resume_daemon_stop_run(
        action,
        mutex=mutex,
        db=db,
        context=context,
        services=services,
    )
    if resume_result.handled:
        return resume_result.run_id
    try:
        raw_run_id: object = spawn_agent(action, db=db, context=context, services=services)
        if inspect.isawaitable(raw_run_id):
            raw_run_id = await cast(Awaitable[str | None], raw_run_id)
    except DispatchSpawnUnavailable:
        mutex.release()
        raise
    except DispatchSpawnFailed as exc:
        _handle_spawn_failure(
            action,
            mutex=mutex,
            db=db,
            context=context,
            error=str(exc),
            cited_subtasks=exc.stage_failure_cited_subtasks,
        )
        return None
    except BaseException:
        if mutex.run_id is None:
            mutex.release()
        raise
    if raw_run_id:
        mutex.attach(str(raw_run_id))
        return str(raw_run_id)
    _handle_spawn_failure(action, mutex=mutex, db=db, context=context, error="missing run_id")
    return None


async def execute_action(
    action: Action,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None = None,
    services: object | None = None,
) -> object | None:
    """Execute one dispatcher action under an acquired lease."""
    if isinstance(action, SpawnAgentAction):
        return await _execute_spawn_action(
            action,
            mutex=mutex,
            db=db,
            context=context,
            services=services,
        )

    if isinstance(action, StartPipelineAction):
        return await _start_pipeline_action(
            action, mutex=mutex, db=db, context=context, services=services
        )

    try:
        if isinstance(action, StartStageAction):
            manager = _stage_states_manager(db=db, services=services)
            return cast(
                object,
                manager.start_stage(
                    action.task_id,
                    action.stage_name,
                    by_session_id="dispatcher",
                ),
            )
        if isinstance(action, AdvanceStageAction):
            manager = _stage_states_manager(db=db, services=services)
            if action.method == "complete_stage":
                return cast(
                    object,
                    manager.complete_stage(
                        action.task_id,
                        action.stage_name,
                        by_session_id=action.by_session_id,
                        validation_override_reason=action.validation_override_reason,
                    ),
                )
            if action.method == "approve_review":
                return cast(
                    object,
                    manager.approve_review(
                        action.task_id,
                        action.stage_name,
                        by_session_id=action.by_session_id,
                    ),
                )
        if isinstance(action, AppendAuditMarkerAction):
            return append_audit_marker(db, action.task_id, action.heading, action.body)
        if isinstance(action, EscalateAction):
            return escalate_task(db=db, task_id=action.task_id, reason=action.reason)
        if isinstance(action, MergeWorkspaceAction):
            return cast(object, await execute_merge_workspace(action, db=db, services=services))
        if isinstance(action, CreateIsolationAction):
            return cast(object | None, create_isolation(action, db=db, context=context))
        raise TypeError(f"Unsupported dispatcher action: {type(action).__name__}")
    finally:
        mutex.release()


async def _start_pipeline_action(
    action: StartPipelineAction,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None,
    services: object | None,
) -> dict[str, object]:
    executor = getattr(services, "pipeline_executor", None)
    loader = getattr(services, "workflow_loader", None) or getattr(executor, "loader", None)
    if executor is None:
        return _escalate_pipeline_dispatch(action, mutex, db, "pipeline_executor_missing")
    if loader is None:
        return _escalate_pipeline_dispatch(action, mutex, db, "pipeline_loader_missing")

    try:
        pipeline = await loader.load_pipeline(action.pipeline_name)
    except ValueError as exc:
        return _escalate_pipeline_dispatch(action, mutex, db, f"pipeline_invalid:{exc}")
    if pipeline is None:
        return _escalate_pipeline_dispatch(
            action, mutex, db, f"pipeline_missing:{action.pipeline_name}"
        )
    if not getattr(pipeline, "enabled", True):
        return _escalate_pipeline_dispatch(
            action, mutex, db, f"pipeline_disabled:{action.pipeline_name}"
        )
    if getattr(pipeline, "deprecated", False):
        return _escalate_pipeline_dispatch(
            action, mutex, db, f"pipeline_deprecated:{action.pipeline_name}"
        )

    try:
        inputs = _render_dispatch_inputs(action, context, services)
    except ValueError as exc:
        return _escalate_pipeline_dispatch(action, mutex, db, f"pipeline_render_failed:{exc}")

    try:
        execution_id = _create_stage_pipeline_execution(
            action,
            pipeline=pipeline,
            inputs=inputs,
            mutex=mutex,
            db=db,
            services=services,
        )
    except _PIPELINE_ATTACH_DATABASE_ERRORS as exc:
        return _escalate_pipeline_dispatch(
            action, mutex, db, f"pipeline_attach_failed:database:{exc}"
        )
    except RuntimeDispatchMutexError as exc:
        return _retry_neutral_pipeline_dispatch(action, mutex, db, str(exc))
    except RuntimeError as exc:
        return _escalate_pipeline_dispatch(action, mutex, db, f"pipeline_attach_failed:{exc}")
    except Exception as exc:
        logger.exception(
            "Unexpected pipeline attach failure for task %s pipeline %s",
            action.task_id,
            action.pipeline_name,
        )
        return _escalate_pipeline_dispatch(
            action, mutex, db, f"pipeline_attach_failed:unexpected:{exc}"
        )
    task = asyncio.create_task(
        _execute_pipeline_background(
            executor,
            pipeline,
            inputs,
            str(_field(context, "project_id", "")),
            execution_id,
            action.pipeline_name,
            session_id=getattr(services, "triggering_session_id", None),
        ),
        name=f"stage-pipeline-{action.pipeline_name}-{execution_id[:8]}",
    )
    _register_background_task(task)
    return {"success": True, "execution_id": execution_id, "status": "running"}


def _escalate_pipeline_dispatch(
    action: StartPipelineAction,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    reason: str,
) -> dict[str, object]:
    escalate_task(db=db, task_id=action.task_id, reason=f"stage_pipeline_dispatch:{reason}")
    mutex.release()
    return {"success": False, "error": reason}


def _retry_neutral_pipeline_dispatch(
    action: StartPipelineAction,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    reason: str,
) -> dict[str, object]:
    mutex.release()
    _restore_stage_pipeline_retry(db, action.task_id, action.stage_name, reason=reason)
    return {"success": False, "error": reason, "retry_neutral": True}


def _restore_stage_pipeline_retry(
    db: HubDatabase,
    task_id: str,
    stage_name: str,
    *,
    reason: str,
) -> bool:
    stage = _stage_states_manager(db=db, services=None).get(task_id, stage_name)
    if stage is None or stage.state != "in_progress":
        return False
    now = datetime.now(UTC).isoformat()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE task_stage_states
               SET state = 'ready',
                   entered_at = NULL,
                   entered_by_session_id = NULL,
                   artifact_refs = NULL,
                   notes = NULL,
                   work_attempt_count = CASE
                       WHEN work_attempt_count > 0 THEN work_attempt_count - 1
                       ELSE 0
                   END,
                   updated_at = %s
             WHERE task_id = %s
               AND stage_name = %s
               AND state = 'in_progress'
            """,
            (now, task_id, stage_name),
        )
        restored = cursor.rowcount > 0
    if restored:
        TaskLifecycleEventManager(db).record_lifecycle_event(
            task_id,
            f"{stage_name}:in_progress",
            f"{stage_name}:ready",
            f"stage_pipeline_dispatch_retry_neutral:{reason}",
            by_actor="dispatcher",
        )
    return restored


def _render_dispatch_inputs(
    action: StartPipelineAction,
    context: object | None,
    services: object | None,
) -> dict[str, Any]:
    render_context = _pipeline_render_context(action, context, services)
    renderer = StepRenderer(TemplateEngine())
    return renderer.render_mcp_arguments(dict(action.dispatch_inputs or {}), render_context)


def _pipeline_render_context(
    action: StartPipelineAction,
    context: object | None,
    services: object | None,
) -> dict[str, Any]:
    task = _field(context, "task")
    artifacts = _field(context, "artifacts", {})
    children = _field(context, "children", [])
    stage_state = _field(context, "current_stage")
    project_id = _field(task, "project_id", _field(context, "project_id"))
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


def _create_stage_pipeline_execution(
    action: StartPipelineAction,
    *,
    pipeline: object,
    inputs: dict[str, Any],
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    services: object | None,
) -> str:
    execution_id = generate_prefixed_id("pe")
    session_id = getattr(services, "triggering_session_id", None)
    try:
        definition_json = cast(Any, pipeline).model_dump_json()
    except Exception:
        definition_json = json.dumps(
            {"name": action.pipeline_name, "error": "serialization failed"}
        )
    with db.transaction_immediate() as conn:
        conn.execute(
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


def _stage_states_manager(*, db: HubDatabase, services: object | None) -> StageStatesManager:
    task_manager = getattr(services, "task_manager", None)
    manager = getattr(task_manager, "stage_states", None)
    if manager is not None:
        return cast(StageStatesManager, manager)
    return StageStatesManager(db, TaskLifecycleEventManager(db))


def count_active_agents(db: HubDatabase | None, project_id: str | None = None) -> int:
    """Return pending/running agent runs, optionally scoped by parent-session project."""
    if db is None:
        return 0
    if project_id:
        row = db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM agent_runs ar
            JOIN sessions parent_s ON parent_s.id = ar.parent_session_id
            WHERE ar.status IN ('pending', 'running')
              AND parent_s.project_id = %s
            """,
            (project_id,),
        )
    else:
        row = db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM agent_runs
            WHERE status IN ('pending', 'running')
            """
        )
    return int(row["count"]) if row else 0


def _handle_spawn_failure(
    action: SpawnAgentAction,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None,
    error: str,
    cited_subtasks: Sequence[str] | None = None,
) -> None:
    try:
        append_audit_marker(db, action.task_id, "Dispatch spawn failed", error)
        cited_subtask_ids = tuple(cited_subtasks or ())
        failure_count = 0
        if not cited_subtask_ids:
            task = get_task(db, action.task_id)
            failure_count = int(getattr(task, "dispatch_failure_count", 0) or 0) + 1
            update_task(db, action.task_id, dispatch_failure_count=failure_count)
        stage = _field(context, "current_stage")
        stage_name = _stage_name(stage)
        if stage_name and _field(stage, "state") == "in_progress":
            failure_reason = (
                f"dispatch_spawn_failed:{error}" if cited_subtask_ids else "dispatch_spawn_failed"
            )
            try:
                _stage_states_manager(
                    db=db,
                    services=getattr(context, "services", None),
                ).fail_stage(
                    action.task_id,
                    stage_name,
                    reason=failure_reason,
                    by_session_id="dispatcher",
                    cited_subtasks=cited_subtask_ids,
                )
            except IllegalStageTransitionError:
                fresh_stage = _stage_states_manager(
                    db=db,
                    services=getattr(context, "services", None),
                ).get(action.task_id, stage_name)
                if fresh_stage is None or fresh_stage.state != "ready":
                    raise
                logger.info(
                    "Dispatch spawn failure rollback already applied: task_id=%s stage_name=%s",
                    action.task_id,
                    stage_name,
                )
            except Exception:
                logger.warning(
                    "Failed to roll back stage after dispatch spawn failure: "
                    "task_id=%s stage_name=%s by_session_id=%s",
                    action.task_id,
                    stage_name,
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


def allocate_expansion_run_id() -> str:
    return str(uuid.uuid4())


def sweep_expired_leases(storage: TaskDispatchMutexManager) -> int:
    active_run_ids = {run.id for run in LocalAgentRunManager(storage.db).list_active(limit=1000)}
    rows = storage.db.fetchall(
        """
        SELECT task_id, run_id
          FROM task_dispatch_mutex
         WHERE lease_until IS NOT NULL
           AND lease_until < %s
        """,
        (datetime.now(UTC).isoformat(),),
    )
    cleared = 0
    for row in rows:
        if row["run_id"] in active_run_ids:
            continue
        if storage.force_release(row["task_id"]):
            cleared += 1
    return cleared


def create_isolation(
    action: CreateIsolationAction,
    *,
    db: HubDatabase,
    context: object | None = None,
) -> TaskArtifacts | None:
    artifacts = getattr(context, "artifacts", None)
    target_branch = action.base_branch or getattr(artifacts, "target_branch", None)
    if not target_branch:
        append_audit_marker(
            db,
            action.task_id,
            "Dispatcher isolation failed",
            "Missing task_artifacts.target_branch.",
        )
        escalate_task(db=db, task_id=action.task_id, reason="isolation_missing_target_branch")
        return None

    base_commit_sha = resolve_branch_sha(str(target_branch))
    if action.isolation == "worktree":
        return set_artifacts_atomic(
            db=db,
            task_id=action.task_id,
            worktree_path=f".gobby/worktrees/{action.task_ref.lstrip('#')}",
            worktree_id=str(uuid.uuid4()),
            base_commit_sha=base_commit_sha,
        )
    if action.isolation == "clone":
        return set_artifacts_atomic(
            db=db,
            task_id=action.task_id,
            clone_path=f".gobby/clones/{action.task_ref.lstrip('#')}",
            clone_id=str(uuid.uuid4()),
            base_commit_sha=base_commit_sha,
        )
    return None


def resolve_branch_sha(branch: str) -> str:
    return branch


def set_artifacts_atomic(
    *,
    db: HubDatabase,
    task_id: str,
    **fields: str | int | None,
) -> TaskArtifacts:
    return _set_artifacts_atomic(db, task_id, **fields)


def escalate_task(*, db: HubDatabase, task_id: str, reason: str) -> bool:
    _escalate_task(db, task_id, reason=reason)
    return True


def _candidate_stage_snapshot(
    candidate: object | None,
) -> tuple[str | None, RuntimeStageSnapshotState | None, str | None]:
    stage = _candidate_current_stage(candidate)
    return _stage_name(stage), _stage_snapshot_state(stage), _stage_updated_at(stage)


def _candidate_current_stage(candidate: object | None) -> object | None:
    if candidate is None:
        return None
    current_stage = _field(candidate, "current_stage")
    if current_stage is not None:
        return current_stage
    return dispatch_rules.current_stage(candidate)


def _stage_name(stage: object | None) -> str | None:
    value = _field(stage, "stage_name", _field(stage, "name"))
    return value if isinstance(value, str) else None


def _stage_snapshot_state(stage: object | None) -> RuntimeStageSnapshotState | None:
    value = _field(stage, "state")
    if isinstance(value, str) and value in {
        "ready",
        "in_progress",
        "needs_review",
        "review_approved",
    }:
        return cast(RuntimeStageSnapshotState, value)
    return None


def _stage_updated_at(stage: object | None) -> str | None:
    value = _field(stage, "updated_at")
    return value if isinstance(value, str) else None


__all__ = [
    "DISPATCH_HOLDER",
    "DISPATCH_TTL_SECONDS",
    "HeartbeatResult",
    "MAX_ACTIVE_AGENTS",
    "allocate_expansion_run_id",
    "build_context",
    "count_active_agents",
    "create_isolation",
    "execute_action",
    "reload_candidate",
    "run_heartbeat",
    "spawn_agent",
]
