"""Heartbeat scanner for task lifecycle dispatch."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from typing import Any, ParamSpec, TypeVar, cast

import psycopg

from gobby.dispatch import results as dispatch_results
from gobby.dispatch import rules as dispatch_rules
from gobby.dispatch import spawn_actions as _spawn_actions
from gobby.dispatch import stage_pipeline as _stage_pipeline
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
from gobby.dispatch.agent_counts import count_active_agents
from gobby.dispatch.audit import append_audit_marker
from gobby.dispatch.constants import (
    DISPATCH_HOLDER,
    DISPATCH_TTL_SECONDS,
    MAX_ACTIVE_AGENTS,
    ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS,
)
from gobby.dispatch.context import _field, build_context, reload_candidate
from gobby.dispatch.daemon_resume import try_resume_daemon_stop_run
from gobby.dispatch.lease_cleanup import (
    sweep_expired_integration_workspace_leases,
    sweep_expired_leases,
    sweep_orphan_no_run_dispatch_mutexes,
)
from gobby.dispatch.mutex import (
    DispatchCandidateChangedError,
    DispatchMutexUnavailableError,
    RuntimeDispatchMutex,
    RuntimeStageSnapshotState,
)
from gobby.dispatch.spawn import (
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
from gobby.storage.hub.protocol import AgentCapAdmission, HubDatabase
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
from gobby.storage.tasks._live_session_recovery import recover_expired_live_session_claims
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.tasks._transitions import escalate_task as _escalate_task
from gobby.storage.tasks._updates import update_task
from gobby.telemetry.health_metrics import record_automation_event
from gobby.utils.datetime import parse_stored_datetime

logger = logging.getLogger(__name__)
# Heartbeats enter from multiple event loops: the automation loop ticks on the
# daemon loop while the build route drives ticks via asyncio.run on a worker
# thread. An asyncio.Lock binds to one loop on contended acquire, so cross-loop
# exclusion needs a thread lock, acquired without blocking the running loop.
_HEARTBEAT_LOCK = threading.Lock()
_HEARTBEAT_LOCK_POLL_SECONDS = 0.05
_P = ParamSpec("_P")
_T = TypeVar("_T")


async def run_db(func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Run synchronous storage work outside the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


HeartbeatResult = dispatch_results.HeartbeatResult
_cap_reached = dispatch_results.cap_reached
_action_cap_reached = dispatch_results.action_cap_reached
_unavailable = dispatch_results.unavailable


_AGENT_CAP_REACHED = object()


def _skipped(result: HeartbeatResult) -> HeartbeatResult:
    record_automation_event("dispatcher", "skipped")
    return dispatch_results.skipped(result)


def _database_error_aborts_scan(error: psycopg.Error) -> bool:
    """Return whether a database error means the heartbeat connection is unusable."""
    sqlstate = error.sqlstate
    return (sqlstate is None and isinstance(error, psycopg.OperationalError)) or bool(
        sqlstate and (sqlstate.startswith("08") or sqlstate.startswith("57P0"))
    )


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
    explicit_task_ids: tuple[str, ...] | None = None,
) -> HeartbeatResult:
    """Serialize heartbeat entry points before scanning and dispatching tasks."""
    while not _HEARTBEAT_LOCK.acquire(blocking=False):
        await asyncio.sleep(_HEARTBEAT_LOCK_POLL_SECONDS)
    try:
        if db is None:
            from gobby.storage.hub.runtime import runtime_hub_database

            database_context = runtime_hub_database(apply_migrations=False)
            database_stack = ExitStack()
            try:
                owned_db = await run_db(database_stack.enter_context, database_context)
                return await _run_heartbeat_unlocked(
                    db=owned_db,
                    project_id=project_id,
                    startup=startup,
                    max_active_agents=max_active_agents,
                    holder=holder,
                    ttl_seconds=ttl_seconds,
                    services=services,
                    max_actions=max_actions,
                    explicit_task_ids=explicit_task_ids,
                )
            finally:
                await run_db(database_stack.close)
        return await _run_heartbeat_unlocked(
            db=db,
            project_id=project_id,
            startup=startup,
            max_active_agents=max_active_agents,
            holder=holder,
            ttl_seconds=ttl_seconds,
            services=services,
            max_actions=max_actions,
            explicit_task_ids=explicit_task_ids,
        )
    except Exception:
        record_automation_event("dispatcher", "failed")
        raise
    finally:
        _HEARTBEAT_LOCK.release()


async def _run_heartbeat_unlocked(
    *,
    db: HubDatabase,
    project_id: str | None = None,
    startup: bool = False,
    max_active_agents: int | None = None,
    holder: str = DISPATCH_HOLDER,
    ttl_seconds: int = DISPATCH_TTL_SECONDS,
    services: object | None = None,
    max_actions: int | None = None,
    explicit_task_ids: tuple[str, ...] | None = None,
) -> HeartbeatResult:
    """Scan automation candidates, acquire per-task leases, and execute first-match actions."""
    from gobby.agents.readiness import spawn_readiness_blocker

    readiness_reason = spawn_readiness_blocker(services)
    if readiness_reason is not None:
        logger.info("Dispatcher heartbeat skipped: %s", readiness_reason)
        record_automation_event("dispatcher", "skipped")
        return _unavailable(HeartbeatResult(), readiness_reason)

    resolved_db = db
    mutex_storage = TaskDispatchMutexManager(resolved_db)
    if startup:
        await sweep_expired_leases(mutex_storage)
        expired_integration_leases = await run_db(
            sweep_expired_integration_workspace_leases,
            resolved_db,
        )
        if expired_integration_leases:
            logger.info(
                "Dispatcher cleared %d expired integration workspace lease(s)",
                expired_integration_leases,
            )
    orphan_mutexes = await run_db(
        sweep_orphan_no_run_dispatch_mutexes,
        mutex_storage,
        resolved_db,
        project_id=project_id,
    )
    if orphan_mutexes:
        logger.info("Dispatcher cleared %d orphan no-run mutex(es)", orphan_mutexes)
    live_recovery = await run_db(
        recover_expired_live_session_claims,
        resolved_db,
        project_id=project_id,
    )
    if live_recovery.released or live_recovery.escalated or live_recovery.raced:
        logger.info(
            "Dispatcher recovered expired live-session claims: released=%d escalated=%d raced=%d",
            live_recovery.released,
            live_recovery.escalated,
            live_recovery.raced,
        )
    reclaimed = await run_db(sweep_stale_claims, resolved_db, project_id=project_id)
    if reclaimed:
        logger.info("Dispatcher reclaimed %d task(s) from dead sessions", reclaimed)
    lifecycle_monitor = getattr(services, "agent_lifecycle_monitor", None)
    if lifecycle_monitor is not None:
        pending_reaped = await lifecycle_monitor.run_acknowledged_stale_sweeps(
            pending_timeout_minutes=60,
        )
        if pending_reaped:
            logger.info("Dispatcher failed %d stale pending agent run(s)", len(pending_reaped))

    cap = MAX_ACTIVE_AGENTS if max_active_agents is None else max_active_agents
    candidates = await run_db(
        list_automation_candidates,
        resolved_db,
        project_id=project_id,
        explicit_task_ids=explicit_task_ids,
    )
    write_set_guard = await run_db(DispatchWriteSetGuard.load, resolved_db, project_id=project_id)
    result = HeartbeatResult(scanned=len(candidates))

    for candidate in candidates:
        if _action_cap_reached(result, max_actions):
            return _cap_reached(result)
        if await run_db(count_active_agents, resolved_db) >= cap:
            return _cap_reached(result)

        snapshot_candidate = await run_db(
            _candidate_for_stage_snapshot,
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
            await run_db(mutex.__enter__)
        except (DispatchMutexUnavailableError, DispatchCandidateChangedError):
            result = _skipped(result)
            continue

        try:
            current = await run_db(
                reload_candidate, candidate.id, db=resolved_db, project_id=project_id
            )
            if current is None or not _candidate_matches_mutex_snapshot(mutex, current):
                result = await _release_and_skip(mutex, result)
                continue

            context = await run_db(build_context, resolved_db, current, services=services)
            if await run_db(
                find_child_development_ancestor_gate,
                resolved_db,
                current,
                current_stage=getattr(context, "current_stage", None),
            ):
                result = await _release_and_skip(mutex, result)
                continue
            action = dispatch_rules.evaluate(current, context, _rules())
            if action is None:
                result = await _release_and_skip(mutex, result)
                continue
            if write_set_guard.action_reserves_write_set(action, current):
                overlap = write_set_guard.conflict_for(action.task_id)
                if overlap is not None:
                    _log_write_set_overlap(overlap)
                    result = await _release_and_skip(mutex, result)
                    continue

            action_result = await _execute_action_with_agent_cap(
                action,
                mutex=mutex,
                db=resolved_db,
                context=context,
                services=services,
                project_id=project_id,
                cap=cap,
            )
            if action_result is _AGENT_CAP_REACHED:
                return _cap_reached(result)
            if action_result is not None and write_set_guard.action_reserves_write_set(
                action, current
            ):
                write_set_guard.reserve(action.task_id)
            result = replace(result, executed=result.executed + 1)
            record_automation_event("dispatcher", "succeeded")
        except DispatchSpawnUnavailable as exc:
            await run_db(mutex.release)
            logger.info("Dispatcher heartbeat unavailable: %s", exc)
            record_automation_event("dispatcher", "skipped")
            return _unavailable(result, str(exc))
        except Exception as exc:
            if isinstance(exc, (TypeError, AttributeError)) or (
                isinstance(exc, psycopg.Error) and _database_error_aborts_scan(exc)
            ):
                if mutex.run_id is None:
                    await run_db(mutex.release)
                raise
            logger.exception("Dispatcher heartbeat candidate failed: task_id=%s", candidate.id)
            try:
                await append_audit_marker(
                    resolved_db,
                    candidate.id,
                    "Dispatch failed",
                    str(exc),
                )
            except Exception:
                logger.debug("Failed to append dispatch failure audit marker", exc_info=True)
            if mutex.run_id is None:
                await run_db(mutex.release)
            result = dispatch_results.skipped(result)
            record_automation_event("dispatcher", "failed")
            continue
        finally:
            if mutex.run_id is None:
                await run_db(mutex.release)

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


async def _release_and_skip(
    mutex: RuntimeDispatchMutex, result: HeartbeatResult
) -> HeartbeatResult:
    await run_db(mutex.release)
    return _skipped(result)


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


async def _execute_action_with_agent_cap(
    action: Action,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object,
    services: object | None,
    project_id: str | None,
    cap: int,
) -> object | None:
    if not isinstance(action, SpawnAgentAction):
        return await _execute_action(
            action,
            mutex=mutex,
            db=db,
            context=context,
            services=services,
        )

    spawn_failure: DispatchSpawnFailed | None = None
    async with db.advisory_lock(AgentCapAdmission(project_id=None)):
        if count_active_agents(db) >= cap:
            mutex.release()
            return _AGENT_CAP_REACHED
        try:
            return await _execute_action(
                action,
                mutex=mutex,
                db=db,
                context=context,
                services=services,
            )
        except DispatchSpawnFailed as exc:
            spawn_failure = exc

    await _handle_spawn_failure(
        action,
        mutex=mutex,
        db=db,
        context=context,
        error=str(spawn_failure),
        cited_subtasks=spawn_failure.stage_failure_cited_subtasks if spawn_failure else None,
    )
    return None


async def _execute_spawn_action(
    action: SpawnAgentAction,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None,
    services: object | None,
) -> str | None:
    return await _spawn_actions.execute_spawn_action(
        action,
        mutex=mutex,
        db=db,
        context=context,
        services=services,
        run_db=run_db,
        spawn_agent=spawn_agent,
        handle_spawn_failure=_handle_spawn_failure,
        cleanup_unattached_spawned_run=_cleanup_unattached_spawned_run,
        quarantine_unterminated_spawned_run=_quarantine_unterminated_spawned_run,
        try_resume_daemon_stop_run=try_resume_daemon_stop_run,
    )


async def _cleanup_unattached_spawned_run(
    run_id: str,
    *,
    db: HubDatabase,
    error: str,
    completion_registry: object | None = None,
    terminal_services: object | None = None,
) -> bool:
    return await _spawn_actions.cleanup_unattached_spawned_run(
        run_id,
        db=db,
        error=error,
        completion_registry=completion_registry,
        terminal_services=terminal_services,
    )


async def _quarantine_unterminated_spawned_run(
    action: SpawnAgentAction,
    *,
    run_id: str,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    error: str,
) -> None:
    await _spawn_actions.quarantine_unterminated_spawned_run(
        action,
        run_id=run_id,
        mutex=mutex,
        db=db,
        error=error,
        run_db=run_db,
        append_audit_marker=append_audit_marker,
        escalate_task=escalate_task,
    )


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
            await run_db(mutex.release)
            manager = _stage_states_manager(db=db, services=services)
            return await run_db(
                manager.start_stage,
                action.task_id,
                action.stage_name,
                by_session_id="dispatcher",
            )
        if isinstance(action, AdvanceStageAction):
            await run_db(mutex.release)
            manager = _stage_states_manager(db=db, services=services)
            if action.method == "complete_stage":
                return await run_db(
                    manager.complete_stage,
                    action.task_id,
                    action.stage_name,
                    by_session_id=action.by_session_id,
                    validation_override_reason=action.validation_override_reason,
                )
            if action.method == "approve_review":
                return await run_db(
                    manager.approve_review,
                    action.task_id,
                    action.stage_name,
                    by_session_id=action.by_session_id,
                )
        if isinstance(action, AppendAuditMarkerAction):
            return await append_audit_marker(db, action.task_id, action.heading, action.body)
        if isinstance(action, EscalateAction):
            return await run_db(escalate_task, db=db, task_id=action.task_id, reason=action.reason)
        if isinstance(action, MergeWorkspaceAction):
            await run_db(mutex.release)
            return cast(object, await execute_merge_workspace(action, db=db, services=services))
        if isinstance(action, CreateIsolationAction):
            return cast(object | None, await create_isolation(action, db=db, context=context))
        raise TypeError(f"Unsupported dispatcher action: {type(action).__name__}")
    finally:
        await run_db(mutex.release)


async def _start_pipeline_action(
    action: StartPipelineAction,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None,
    services: object | None,
) -> dict[str, object]:
    return await _stage_pipeline.start_pipeline_action(
        action,
        mutex=mutex,
        db=db,
        context=context,
        services=services,
        field=_field,
        escalate_pipeline_dispatch=_escalate_pipeline_dispatch,
        retry_neutral_pipeline_dispatch=_retry_neutral_pipeline_dispatch,
        render_dispatch_inputs=_render_dispatch_inputs,
        create_stage_pipeline_execution=_create_stage_pipeline_execution,
        execute_pipeline_background=_execute_pipeline_background,
        register_background_task=_register_background_task,
    )


def _escalate_pipeline_dispatch(
    action: StartPipelineAction,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    reason: str,
) -> dict[str, object]:
    return _stage_pipeline.escalate_pipeline_dispatch(
        action,
        mutex,
        db,
        reason,
        escalate_task=escalate_task,
    )


def _retry_neutral_pipeline_dispatch(
    action: StartPipelineAction,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    reason: str,
) -> dict[str, object]:
    return _stage_pipeline.retry_neutral_pipeline_dispatch(
        action,
        mutex,
        db,
        reason,
        restore_stage_pipeline_retry=_restore_stage_pipeline_retry,
        escalate_task=escalate_task,
    )


def _restore_stage_pipeline_retry(
    db: HubDatabase,
    task_id: str,
    stage_name: str,
    *,
    reason: str,
) -> int:
    return _stage_pipeline.restore_stage_pipeline_retry(
        db,
        task_id,
        stage_name,
        reason=reason,
        stage_states_manager=_stage_states_manager,
    )


def _render_dispatch_inputs(
    action: StartPipelineAction,
    context: object | None,
    services: object | None,
) -> dict[str, Any]:
    return _stage_pipeline.render_dispatch_inputs(action, context, services, field=_field)


def _pipeline_render_context(
    action: StartPipelineAction,
    context: object | None,
    services: object | None,
) -> dict[str, Any]:
    return _stage_pipeline.pipeline_render_context(action, context, services, field=_field)


def _create_stage_pipeline_execution(
    action: StartPipelineAction,
    *,
    pipeline: object,
    inputs: dict[str, Any],
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    services: object | None,
) -> str:
    return _stage_pipeline.create_stage_pipeline_execution(
        action,
        pipeline=pipeline,
        inputs=inputs,
        mutex=mutex,
        db=db,
        services=services,
    )


def _stage_states_manager(*, db: HubDatabase, services: object | None) -> StageStatesManager:
    task_manager = getattr(services, "task_manager", None)
    manager = getattr(task_manager, "stage_states", None)
    if manager is not None:
        return cast(StageStatesManager, manager)
    return StageStatesManager(db, TaskLifecycleEventManager(db))


async def _handle_spawn_failure(
    action: SpawnAgentAction,
    *,
    mutex: RuntimeDispatchMutex,
    db: HubDatabase,
    context: object | None,
    error: str,
    cited_subtasks: Sequence[str] | None = None,
) -> None:
    await _spawn_actions.handle_spawn_failure(
        action,
        mutex=mutex,
        db=db,
        context=context,
        error=error,
        cited_subtasks=cited_subtasks,
        append_audit_marker=append_audit_marker,
        get_task=get_task,
        update_task=update_task,
        escalate_task=escalate_task,
        field=_field,
        stage_name=_stage_name,
        stage_states_manager=_stage_states_manager,
    )


def allocate_expansion_run_id() -> str:
    return str(uuid.uuid4())


async def create_isolation(
    action: CreateIsolationAction,
    *,
    db: HubDatabase,
    context: object | None = None,
) -> TaskArtifacts | None:
    artifacts = getattr(context, "artifacts", None)
    target_branch = action.base_branch or getattr(artifacts, "target_branch", None)
    if not target_branch:
        await append_audit_marker(
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
) -> tuple[str | None, RuntimeStageSnapshotState | None, datetime | None]:
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


def _stage_updated_at(stage: object | None) -> datetime | None:
    value = _field(stage, "updated_at")
    if isinstance(value, datetime | str):
        return parse_stored_datetime(value)
    return None


__all__ = [
    "DISPATCH_HOLDER",
    "DISPATCH_TTL_SECONDS",
    "HeartbeatResult",
    "MAX_ACTIVE_AGENTS",
    "ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS",
    "allocate_expansion_run_id",
    "build_context",
    "count_active_agents",
    "create_isolation",
    "execute_action",
    "reload_candidate",
    "run_heartbeat",
    "spawn_agent",
    "sweep_expired_leases",
    "sweep_orphan_no_run_dispatch_mutexes",
]
