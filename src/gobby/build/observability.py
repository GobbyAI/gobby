"""Read-only build observability service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.dispatch import rules as dispatch_rules
from gobby.dispatch.actions import (
    AdvanceStageAction,
    AppendAuditMarkerAction,
    CreateIsolationAction,
    EscalateAction,
    MergeWorkspaceAction,
    SpawnAgentAction,
    StartPipelineAction,
    StartStageAction,
)
from gobby.dispatch.context import build_context, reload_candidate
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.build_history import BuildHistoryStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._ancestor_gate import (
    AncestorStageGate,
    find_child_development_ancestor_gate,
)
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

MAX_ACTIVE_AGENTS = 10


def get_build_status(
    input_ref: str,
    *,
    db: HubDatabase,
    project_id: str,
    history_limit: int = 5,
) -> dict[str, Any]:
    """Return compact state for a build root and its task subtree."""
    task_manager = LocalTaskManager(db)
    root = _resolve_root(input_ref, db=db, project_id=project_id, task_manager=task_manager)
    tasks = _subtree_tasks(task_manager, root)
    task_ids = [task.id for task in tasks]
    active_agents = LocalAgentRunManager(db).list_active(task_ids=task_ids, limit=1000)
    history = BuildHistoryStorage(db)
    recent_history = history.list_runs(
        project_id=project_id,
        root_task_id=root.id,
        limit=history_limit,
    )

    return {
        "ok": True,
        "root": _task_summary(root),
        "summary": _build_summary(task_manager, root, tasks, active_agents),
        "tasks": [
            _task_status(task_manager, task, active_agents)
            for task in sorted(tasks, key=_task_order_key)
        ],
        "agents": [_agent_summary(run) for run in active_agents],
        "mutexes": _mutex_summaries(db, task_ids),
        "artifact_health": _artifact_health(task_manager, tasks),
        "recent_events": _recent_lifecycle_events(db, task_ids, limit=history_limit),
        "recent_history": [run.to_dict() for run in recent_history],
        "resume_summary": _resume_summary(task_manager, tasks, active_agents),
    }


def explain_dispatch(
    task_id: str,
    *,
    db: HubDatabase,
    project_id: str,
    max_active_agents: int | None = None,
    services: object | None = None,
) -> dict[str, Any]:
    """Explain what the dispatcher would do for one task without mutating state."""
    task_manager = LocalTaskManager(db)
    task = task_manager.get_task(task_id, project_id=project_id)
    candidate = reload_candidate(task.id, db=db, project_id=project_id) or task
    current_stage = dispatch_rules.current_stage(candidate)
    mutex = _mutex_diagnosis(db, task.id)
    cap = max_active_agents or MAX_ACTIVE_AGENTS
    active_count = _count_active_agents(db, project_id=project_id)
    active_agents = {
        "count": active_count,
        "cap": cap,
        "cap_reached": active_count >= cap,
    }
    ancestor_gate = find_child_development_ancestor_gate(
        db,
        candidate,
        current_stage=current_stage,
    )

    reason = _dispatch_block_reason(
        candidate,
        current_stage,
        mutex,
        active_agents,
        ancestor_gate,
    )
    action = None
    if reason is None:
        context = build_context(db, candidate, services=services)
        action = dispatch_rules.evaluate(candidate, context, dispatch_rules.RULES)
        if action is None:
            reason = "no_matching_rule"

    return {
        "ok": True,
        "task": _task_summary(task),
        "eligible": reason is None,
        "reason": reason,
        "inputs": _dispatch_inputs(candidate),
        "current_stage": _stage_summary(current_stage),
        "ancestor_gate": ancestor_gate.to_dict() if ancestor_gate is not None else None,
        "mutex": mutex,
        "active_agents": active_agents,
        "proposed_action": _action_summary(action),
    }


def list_build_history(
    input_ref: str,
    *,
    db: HubDatabase,
    project_id: str,
    limit: int = 20,
) -> dict[str, Any]:
    """List recent build run and event rows for a task ref or input path."""
    history = BuildHistoryStorage(db)
    root_task_id: str | None = None
    root = _try_resolve_root(input_ref, db=db, project_id=project_id)
    if root is not None:
        root_task_id = root.id
        runs = history.list_runs(project_id=project_id, root_task_id=root.id, limit=limit)
        events = history.list_events(project_id=project_id, root_task_id=root.id, limit=limit)
    else:
        latest = history.latest_run_for_input(project_id, input_ref)
        root_task_id = latest.root_task_id if latest else None
        runs = history.list_runs(project_id=project_id, input_ref=input_ref, limit=limit)
        events = history.list_events(project_id=project_id, input_ref=input_ref, limit=limit)
    return {
        "ok": True,
        "root_task_id": root_task_id,
        "runs": [run.to_dict() for run in runs],
        "events": [event.to_dict() for event in events],
    }


def _resolve_root(
    input_ref: str,
    *,
    db: HubDatabase,
    project_id: str,
    task_manager: LocalTaskManager,
) -> Task:
    task = _try_resolve_root(input_ref, db=db, project_id=project_id)
    if task is not None:
        return task
    run = BuildHistoryStorage(db).latest_run_for_input(project_id, input_ref)
    if run is not None and run.root_task_id:
        return task_manager.get_task(run.root_task_id, project_id=project_id)
    raise ValueError(f"build input not found: {input_ref}")


def _try_resolve_root(input_ref: str, *, db: HubDatabase, project_id: str) -> Task | None:
    try:
        return LocalTaskManager(db).get_task(input_ref, project_id=project_id)
    except Exception:
        return None


def _subtree_tasks(task_manager: LocalTaskManager, root: Task) -> list[Task]:
    if root.task_type != "epic":
        return [root]
    rows = task_manager.db.fetchall(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id
              FROM tasks
             WHERE id = %s
            UNION ALL
            SELECT child.id
              FROM tasks child
              JOIN subtree parent ON child.parent_task_id = parent.id
        )
        SELECT id
          FROM subtree
        """,
        (root.id,),
    )
    return [task_manager.get_task(row["id"]) for row in rows]


def _build_summary(
    task_manager: LocalTaskManager,
    root: Task,
    tasks: Sequence[Task],
    active_agents: Sequence[Any],
) -> dict[str, Any]:
    task_ids = [task.id for task in tasks]
    built = task_manager.lifecycle_events.tasks_with_build_event(task_ids)
    return {
        "state": _root_build_state(task_manager, root),
        "total_tasks": len(tasks),
        "open_tasks": len([task for task in tasks if task.closed_at is None]),
        "closed_tasks": len([task for task in tasks if task.closed_at is not None]),
        "escalated_tasks": len([task for task in tasks if task.is_escalated]),
        "automation_enabled_tasks": len([task for task in tasks if task.allow_automation]),
        "blocked_tasks": len([task for task in tasks if task.active_blocked_by]),
        "active_agents": len(active_agents),
        "tasks_with_build_event": len(built),
    }


def _root_build_state(task_manager: LocalTaskManager, root: Task) -> str:
    if root.closed_at is not None:
        return "completed"
    if root.allow_automation:
        return "running"
    if task_manager.lifecycle_events.has_build_event(root.id):
        return "paused"
    return "never_started"


def _task_status(
    task_manager: LocalTaskManager,
    task: Task,
    active_agents: Sequence[Any],
) -> dict[str, Any]:
    return {
        **_task_summary(task),
        "parent_task_id": task.parent_task_id,
        "category": task.category,
        "closed": task.closed_at is not None,
        "closed_at": task.closed_at,
        "escalated": task.is_escalated,
        "escalation_reason": task.escalation_reason,
        "allow_automation": task.allow_automation,
        "unattended": task.unattended,
        "isolation": task.isolation.value,
        "claimed_by_session_id": task.claimed_by_session_id,
        "active_blocked_by": sorted(task.active_blocked_by),
        "current_stage": _stage_summary(dispatch_rules.current_stage(task)),
        "stages": [_stage_summary(stage) for stage in task.stages],
        "active_agents": [
            _agent_summary(run) for run in active_agents if getattr(run, "task_id", None) == task.id
        ],
        "has_build_event": task_manager.lifecycle_events.has_build_event(task.id),
        "latest_failure_comment": _latest_failure_comment(task_manager.db, task.id),
        "latest_audit_marker": _latest_audit_marker(task.description),
    }


def _task_summary(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "ref": f"#{task.seq_num}" if task.seq_num else task.id,
        "path": task.path_cache,
        "title": task.title,
        "task_type": task.task_type,
    }


def _stage_summary(stage: object | None) -> dict[str, Any] | None:
    if stage is None:
        return None
    return {
        "stage_name": _field(stage, "stage_name"),
        "position": _field(stage, "position"),
        "state": _field(stage, "state"),
        "review_policy": _field(stage, "review_policy"),
        "reviewer_agent": _field(stage, "reviewer_agent"),
        "work_attempt_count": _field(stage, "work_attempt_count"),
        "review_round_count": _field(stage, "review_round_count"),
        "max_work_attempts": _field(stage, "max_work_attempts"),
        "max_review_rounds": _field(stage, "max_review_rounds"),
        "updated_at": _field(stage, "updated_at"),
    }


def _agent_summary(run: Any) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "task_id": run.task_id,
        "status": run.status,
        "agent_name": run.agent_name,
        "workflow_name": run.workflow_name,
        "provider": run.provider,
        "model": run.model,
        "child_session_id": run.child_session_id,
        "claimed_session_id": run.claimed_session_id,
        "worktree_id": run.worktree_id,
        "clone_id": run.clone_id,
        "started_at": run.started_at,
        "created_at": run.created_at,
    }


def _count_active_agents(db: HubDatabase, project_id: str | None) -> int:
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
    return int(row["count"]) if row is not None else 0


def _mutex_summaries(db: HubDatabase, task_ids: Sequence[str]) -> list[dict[str, Any]]:
    return [
        diagnosis
        for task_id in task_ids
        if (diagnosis := _mutex_diagnosis(db, task_id))["state"] != "none"
    ]


def _mutex_diagnosis(db: HubDatabase, task_id: str) -> dict[str, Any]:
    mutex = TaskDispatchMutexManager(db).get_mutex(task_id)
    if mutex is None:
        return {"task_id": task_id, "state": "none", "blocks_dispatch": False}
    now = datetime.now(UTC)
    lease_until = _parse_time(mutex.lease_until)
    active_run_ids = {run.id for run in LocalAgentRunManager(db).list_active(limit=1000)}
    expired = lease_until is not None and lease_until <= now
    run_active = bool(mutex.run_id and mutex.run_id in active_run_ids)
    no_run = mutex.run_id is None
    if run_active and expired:
        state = "active_run_expired_lease"
    elif expired:
        state = "expired"
    elif no_run:
        state = "active_no_run"
    elif run_active:
        state = "active_run"
    else:
        state = "stale_run"
    return {
        "task_id": task_id,
        "state": state,
        "blocks_dispatch": state in {"active_no_run", "active_run", "active_run_expired_lease"},
        "lease_expired": expired,
        "run_active": run_active,
        "lease_until": mutex.lease_until,
        "lease_holder": mutex.lease_holder,
        "run_id": mutex.run_id,
        "action_kind": mutex.action_kind,
        "updated_at": mutex.updated_at,
    }


def _artifact_health(task_manager: LocalTaskManager, tasks: Sequence[Task]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    issue_count = 0
    for task in tasks:
        artifacts = task_manager.artifacts.get_artifacts(task.id)
        paths = {
            "worktree_path": artifacts.worktree_path,
            "clone_path": artifacts.clone_path,
        }
        task_items = []
        for name, raw_path in paths.items():
            if not raw_path:
                continue
            exists = Path(raw_path).expanduser().exists()
            if not exists:
                issue_count += 1
            task_items.append({"field": name, "path": raw_path, "exists": exists})
        if task_items:
            items.append({"task_id": task.id, "artifacts": task_items})
    return {"ok": issue_count == 0, "issue_count": issue_count, "items": items}


def _recent_lifecycle_events(
    db: HubDatabase,
    task_ids: Sequence[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not task_ids:
        return []
    placeholders = ", ".join("%s" for _ in task_ids)
    rows = db.fetchall(
        f"""
        SELECT id, task_id, from_state, to_state, reason, by_actor, created_at
          FROM task_lifecycle_events
         WHERE task_id IN ({placeholders})
         ORDER BY created_at DESC, id DESC
         LIMIT %s
        """,  # nosec B608
        (*task_ids, _bounded_limit(limit)),
    )
    return [dict(row) for row in rows]


def _resume_summary(
    task_manager: LocalTaskManager,
    tasks: Sequence[Task],
    active_agents: Sequence[Any],
) -> dict[str, Any]:
    built = task_manager.lifecycle_events.tasks_with_build_event([task.id for task in tasks])
    paused = [
        task
        for task in tasks
        if task.id in built
        and not task.allow_automation
        and task.closed_at is None
        and not task.is_escalated
    ]
    return {
        "can_resume": bool(paused) and not active_agents,
        "paused_tasks": [f"#{task.seq_num}" if task.seq_num else task.id for task in paused],
        "active_agents": len(active_agents),
    }


def _dispatch_block_reason(
    task: Task,
    current_stage: object | None,
    mutex: Mapping[str, Any],
    active_agents: Mapping[str, Any],
    ancestor_gate: AncestorStageGate | None,
) -> str | None:
    if task.closed_at is not None:
        return "closed"
    if task.is_escalated:
        return "escalated"
    if not task.allow_automation:
        return "automation_disabled"
    if task.claimed_by_session_id:
        return "claimed"
    if mutex.get("blocks_dispatch"):
        return "active_mutex"
    if task.active_blocked_by:
        return "dependency_block"
    if ancestor_gate is not None:
        return ancestor_gate.reason
    if current_stage is None:
        return "no_current_stage"
    if _field(current_stage, "state") not in {
        "ready",
        "in_progress",
        "needs_review",
        "review_approved",
    }:
        return "stage_not_dispatchable"
    if active_agents["cap_reached"]:
        return "active_agent_cap_reached"
    return None


def _dispatch_inputs(task: Task) -> dict[str, Any]:
    return {
        "allow_automation": task.allow_automation,
        "claimed_by_session_id": task.claimed_by_session_id,
        "closed_at": task.closed_at,
        "is_escalated": task.is_escalated,
        "active_blocked_by": sorted(task.active_blocked_by),
        "isolation": task.isolation.value,
        "assigned_agent": task.assigned_agent,
        "dispatch_failure_count": task.dispatch_failure_count,
    }


def _action_summary(action: object | None) -> dict[str, Any] | None:
    if action is None:
        return None
    if isinstance(action, SpawnAgentAction):
        return {
            "action": "spawn_agent",
            "task_id": action.task_id,
            "task_ref": action.task_ref,
            "agent_slug": action.agent_slug,
            "additional_skills": list(action.additional_skills),
            "model_override": action.model_override,
            "reasoning_effort": action.reasoning_effort,
        }
    if isinstance(action, StartPipelineAction):
        return {"action": "start_pipeline", **asdict(action)}
    if isinstance(action, StartStageAction):
        return {"action": "start_stage", **asdict(action)}
    if isinstance(action, CreateIsolationAction):
        return {"action": "create_isolation", **asdict(action)}
    if isinstance(action, MergeWorkspaceAction):
        return {"action": "merge_workspace", **asdict(action)}
    if isinstance(action, AdvanceStageAction):
        return {"action": "advance_stage", **asdict(action)}
    if isinstance(action, AppendAuditMarkerAction):
        return {"action": "append_audit_marker", **asdict(action)}
    if isinstance(action, EscalateAction):
        return {"action": "escalate", **asdict(action)}
    return {"action": type(action).__name__}


def _latest_failure_comment(db: HubDatabase, task_id: str) -> dict[str, Any] | None:
    row = db.fetchone(
        """
        SELECT id, task_id, author, author_type, body, created_at
          FROM task_comments
         WHERE task_id = %s
           AND author_type = 'system'
           AND (
               body LIKE '%%Failure%%'
               OR body LIKE '%%Follow-Up%%'
               OR body LIKE '%%Dispatch%%'
               OR body LIKE '%%Audit%%'
           )
         ORDER BY created_at DESC
         LIMIT 1
        """,
        (task_id,),
    )
    return dict(row) if row is not None else None


def _latest_audit_marker(description: str | None) -> dict[str, str] | None:
    if not description or "### " not in description:
        return None
    markers = [part.strip() for part in description.split("### ") if part.strip()]
    for marker in reversed(markers):
        heading, _, body = marker.partition("\n")
        if any(token in heading.lower() for token in ("dispatch", "audit", "failure")):
            return {"heading": heading.strip(), "body": body.strip()}
    return None


def _field(obj: object | None, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _task_order_key(task: Task) -> tuple[str, int, str]:
    return (task.path_cache or "", task.seq_num or 0, task.id)


def _bounded_limit(value: int) -> int:
    return max(1, min(int(value), 100))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = [
    "explain_dispatch",
    "get_build_status",
    "list_build_history",
]
