"""Heartbeat scanner for task lifecycle dispatch."""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

from gobby.dispatch import rules as dispatch_rules
from gobby.dispatch.actions import (
    Action,
    AdvanceLifecycleAction,
    AdvanceStageAction,
    AppendAuditMarkerAction,
    CreateIsolationAction,
    EscalateAction,
    SpawnAgentAction,
    StartExpansionAction,
    StartStageAction,
)
from gobby.dispatch.mutex import (
    DispatchCandidateChangedError,
    DispatchMutexUnavailableError,
    RuntimeDispatchMutex,
    RuntimeStageSnapshotState,
)
from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl
from gobby.storage.database import DatabaseProtocol, LocalDatabase
from gobby.storage.tasks._artifacts import (
    TaskArtifactManager,
    TaskArtifacts,
)
from gobby.storage.tasks._artifacts import (
    set_artifacts_atomic as _set_artifacts_atomic,
)
from gobby.storage.tasks._crud import get_task, list_automation_candidates, update_task
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._stage_states import StageState, StageStatesManager
from gobby.storage.tasks._transitions import advance_lifecycle

MAX_ACTIVE_AGENTS = 10
DISPATCH_HOLDER = "dispatcher"
DISPATCH_TTL_SECONDS = 600


@dataclass(frozen=True)
class HeartbeatResult:
    scanned: int = 0
    executed: int = 0
    skipped: int = 0
    cap_reached: bool = False


async def run_heartbeat(
    *,
    db: DatabaseProtocol | None = None,
    project_id: str | None = None,
    startup: bool = False,
    max_active_agents: int | None = None,
    holder: str = DISPATCH_HOLDER,
    ttl_seconds: int = DISPATCH_TTL_SECONDS,
    services: object | None = None,
) -> HeartbeatResult:
    """Scan automation candidates, acquire per-task leases, and execute first-match actions."""
    resolved_db = db or LocalDatabase()
    mutex_storage = TaskDispatchMutexManager(resolved_db)
    if startup:
        sweep_expired_leases(mutex_storage)

    cap = MAX_ACTIVE_AGENTS if max_active_agents is None else max_active_agents
    candidates = list_automation_candidates(resolved_db, project_id=project_id)
    result = HeartbeatResult(scanned=len(candidates))

    for candidate in candidates:
        if count_active_agents(resolved_db, project_id=project_id) >= cap:
            return HeartbeatResult(
                scanned=result.scanned,
                executed=result.executed,
                skipped=result.skipped,
                cap_reached=True,
            )

        snapshot_candidate = candidate
        if _candidate_current_stage(snapshot_candidate) is None:
            reloaded = reload_candidate(candidate.id, db=resolved_db, project_id=project_id)
            if reloaded is None:
                result = HeartbeatResult(
                    result.scanned,
                    result.executed,
                    result.skipped + 1,
                )
                continue
            snapshot_candidate = reloaded
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
            result = HeartbeatResult(
                scanned=result.scanned,
                executed=result.executed,
                skipped=result.skipped + 1,
                cap_reached=False,
            )
            continue

        try:
            current = reload_candidate(candidate.id, db=resolved_db, project_id=project_id)
            if current is None or not mutex.candidate_stage_snapshot_matches(
                *_candidate_stage_snapshot(current)
            ):
                mutex.release()
                result = HeartbeatResult(result.scanned, result.executed, result.skipped + 1)
                continue

            context = build_context(resolved_db, current, services=services)
            action = dispatch_rules.evaluate(current, context, _rules())
            if action is None:
                mutex.release()
                result = HeartbeatResult(result.scanned, result.executed, result.skipped + 1)
                continue

            await _execute_action(
                action,
                mutex=mutex,
                db=resolved_db,
                context=context,
                services=services,
            )
            result = HeartbeatResult(result.scanned, result.executed + 1, result.skipped)
        except Exception:
            mutex.release()
            raise

    return result


def _rules() -> list[Any]:
    return list(getattr(dispatch_rules, "RULES", dispatch_rules.BASE_RULES))


def reload_candidate(
    task_id: str,
    *,
    db: DatabaseProtocol | None = None,
    project_id: str | None = None,
) -> Task | None:
    if db is None:
        return None
    where_clause, params = _candidate_lookup_clause(task_id, project_id)
    if where_clause is None:
        return None
    rows = db.fetchall(
        f"""
        SELECT
            t.*,
            s.task_id AS stage_task_id,
            s.stage_name AS stage_name,
            s.position AS stage_position,
            s.state AS stage_state,
            s.review_policy AS stage_review_policy,
            s.reviewer_agent AS stage_reviewer_agent,
            s.entered_at AS stage_entered_at,
            s.entered_by_session_id AS stage_entered_by_session_id,
            s.completed_at AS stage_completed_at,
            s.completed_by_session_id AS stage_completed_by_session_id,
            s.completed_commit_sha AS stage_completed_commit_sha,
            s.work_attempt_count AS stage_work_attempt_count,
            s.review_round_count AS stage_review_round_count,
            s.max_work_attempts AS stage_max_work_attempts,
            s.max_review_rounds AS stage_max_review_rounds,
            s.artifact_refs AS stage_artifact_refs,
            s.notes AS stage_notes,
            s.updated_at AS stage_updated_at
        FROM tasks t
        LEFT JOIN task_stage_states s ON s.task_id = t.id
        WHERE {where_clause}
        ORDER BY s.position, s.stage_name
        """,  # nosec B608 - where_clause is selected from fixed templates.
        tuple(params),
    )
    if not rows:
        return None
    task = Task.from_row(rows[0])
    task.stages = tuple(_stage_from_joined_row(row) for row in rows if row["stage_task_id"])
    return task


def _candidate_lookup_clause(
    task_id: str,
    project_id: str | None,
) -> tuple[str | None, list[object]]:
    if task_id.startswith("#") or task_id.isdigit():
        if project_id is None:
            return None, []
        try:
            seq_num = int(task_id[1:] if task_id.startswith("#") else task_id)
        except ValueError:
            return None, []
        return "t.project_id = ? AND t.seq_num = ?", [project_id, seq_num]

    if "." in task_id and all(part.isdigit() for part in task_id.split(".")):
        if project_id is None:
            return None, []
        return "t.project_id = ? AND t.path_cache = ?", [project_id, task_id]

    params: list[object] = [task_id]
    clause = "t.id = ?"
    if project_id is not None:
        clause += " AND t.project_id = ?"
        params.append(project_id)
    return clause, params


def _stage_from_joined_row(row: Any) -> StageState:
    return StageState(
        task_id=row["stage_task_id"],
        stage_name=row["stage_name"],
        position=int(row["stage_position"]),
        state=row["stage_state"],
        review_policy=row["stage_review_policy"],
        reviewer_agent=row["stage_reviewer_agent"],
        entered_at=row["stage_entered_at"],
        entered_by_session_id=row["stage_entered_by_session_id"],
        completed_at=row["stage_completed_at"],
        completed_by_session_id=row["stage_completed_by_session_id"],
        completed_commit_sha=row["stage_completed_commit_sha"],
        work_attempt_count=int(row["stage_work_attempt_count"]),
        review_round_count=int(row["stage_review_round_count"]),
        max_work_attempts=row["stage_max_work_attempts"],
        max_review_rounds=row["stage_max_review_rounds"],
        artifact_refs=_artifact_refs(row["stage_artifact_refs"]),
        notes=row["stage_notes"],
        updated_at=row["stage_updated_at"],
    )


def _artifact_refs(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        return None
    return {str(key): str(item) for key, item in decoded.items()}


def build_context(
    db: DatabaseProtocol,
    task: Task,
    *,
    services: object | None = None,
) -> object:
    artifacts = TaskArtifactManager(db).get_artifacts(task.id)
    children = _children(db, task.id)
    build_config = getattr(services, "config", None) if services is not None else None
    return SimpleNamespace(
        artifacts=artifacts,
        children=children,
        build_config=build_config,
        db=db,
        services=services,
    )


def _children(db: DatabaseProtocol, task_id: str) -> list[Task]:
    rows = db.fetchall("SELECT * FROM tasks WHERE parent_task_id = ?", (task_id,))
    return [Task.from_row(row) for row in rows]


async def _execute_action(
    action: Action,
    *,
    mutex: RuntimeDispatchMutex,
    db: DatabaseProtocol,
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


def execute_action(
    action: Action,
    *,
    mutex: RuntimeDispatchMutex,
    db: DatabaseProtocol,
    context: object | None = None,
    services: object | None = None,
) -> object | None:
    """Execute one dispatcher action under an acquired lease."""
    if isinstance(action, SpawnAgentAction):
        run_id = spawn_agent(action, db=db, services=services)
        if inspect.isawaitable(run_id):
            raise RuntimeError("async spawn_agent requires monkeypatching _execute_action")
        if run_id:
            mutex.attach(str(run_id))
        return run_id

    if isinstance(action, StartExpansionAction):
        run_id = allocate_expansion_run_id()
        mutex.attach(run_id)
        return start_expansion_run_impl(
            task_id=action.task_id,
            run_id=run_id,
            auto_apply=True,
            task_manager=getattr(services, "task_manager", None),
            llm_service=getattr(services, "llm_service", None),
            config=getattr(services, "config", None),
            completion_registry=getattr(services, "completion_registry", None),
            triggering_session_id=getattr(services, "triggering_session_id", None),
            project=getattr(services, "project", None),
        )

    try:
        if isinstance(action, StartStageAction):
            manager = _stage_states_manager(db=db, services=services)
            return manager.start_stage(
                action.task_id,
                action.stage_name,
                by_session_id="dispatcher",
            )
        if isinstance(action, AdvanceStageAction):
            manager = _stage_states_manager(db=db, services=services)
            if action.method == "complete_stage":
                return manager.complete_stage(
                    action.task_id,
                    action.stage_name,
                    by_session_id=action.by_session_id,
                )
            if action.method == "approve_review":
                return manager.approve_review(
                    action.task_id,
                    action.stage_name,
                    by_session_id=action.by_session_id,
                )
        if isinstance(action, AdvanceLifecycleAction):
            return advance_lifecycle(
                db,
                action.task_id,
                action.to_lifecycle,
                action.to_status,
                {"reason": action.reason, "by_actor": action.by_actor},
            )
        if isinstance(action, AppendAuditMarkerAction):
            return append_audit_marker(db, action.task_id, action.heading, action.body)
        if isinstance(action, EscalateAction):
            return escalate_task(db=db, task_id=action.task_id, reason=action.reason)
        if isinstance(action, CreateIsolationAction):
            return create_isolation(action, db=db, context=context)
        raise TypeError(f"Unsupported dispatcher action: {type(action).__name__}")
    finally:
        mutex.release()


def _stage_states_manager(*, db: DatabaseProtocol, services: object | None) -> StageStatesManager:
    task_manager = getattr(services, "task_manager", None)
    manager = getattr(task_manager, "stage_states", None)
    if manager is not None:
        return cast(StageStatesManager, manager)
    return StageStatesManager(db, TaskLifecycleEventManager(db))


def count_active_agents(*args: object, **kwargs: object) -> int:
    """Return active dispatcher-controlled agents; integration wiring lands with cron."""
    _ = args, kwargs
    return 0


def spawn_agent(
    action: SpawnAgentAction,
    *,
    db: DatabaseProtocol | None = None,
    services: object | None = None,
) -> str:
    """Spawn an agent and return its run id; external wiring can monkeypatch this boundary."""
    _ = action, db, services
    return str(uuid.uuid4())


def allocate_expansion_run_id() -> str:
    return str(uuid.uuid4())


def sweep_expired_leases(storage: TaskDispatchMutexManager) -> int:
    return storage.sweep_expired()


def create_isolation(
    action: CreateIsolationAction,
    *,
    db: DatabaseProtocol,
    context: object | None = None,
) -> TaskArtifacts | None:
    artifacts = cast(TaskArtifacts | None, getattr(context, "artifacts", None))
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
    db: DatabaseProtocol,
    task_id: str,
    **fields: str | int | None,
) -> TaskArtifacts:
    return _set_artifacts_atomic(db, task_id, **fields)


def append_audit_marker(db: DatabaseProtocol, task_id: str, heading: str, body: str) -> bool:
    task = get_task(db, task_id)
    description = task.description or ""
    marker = f"\n\n### {heading}\n\n{body}"
    update_task(db, task_id, description=f"{description}{marker}")
    return True


def escalate_task(*, db: DatabaseProtocol, task_id: str, reason: str) -> bool:
    return update_task(
        db,
        task_id,
        status="escalated",
        escalation_reason=reason,
    )


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


def _field(
    obj: object | None,
    name: str,
    default: object | None = None,
) -> object | None:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return cast(Mapping[str, object | None], obj).get(name, default)
    return getattr(obj, name, default)


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
    "start_expansion_run_impl",
]
