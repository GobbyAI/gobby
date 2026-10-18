"""Fixed standing-seat effects replacing native flag writes.

Only the workflow engine dispatches these effects after successful existing
tools. Each role owns a bounded set of flags; no tool exposes this writer.
"""

from __future__ import annotations

import re
from typing import Any

from gobby.mcp_proxy.tools.task_repo_paths import resolve_task_repo_path
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.tasks.close_receipts import LANDING_APPROVAL, list_close_receipts
from gobby.utils.git import normalize_commit_sha
from gobby.workflows.claimed_task_extra_skills import build_claimed_task_extra_skill_state
from gobby.workflows.engine.seat_messages import (
    CandidateEvent,
    candidate_event,
    completion_messages,
    is_task_coordinator,
    is_task_reviewer,
)
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstance


def _approved(db: HubDatabase, task_id: str, sha: str) -> bool:
    return any(
        receipt.kind == LANDING_APPROVAL and receipt.commit_sha == sha
        for receipt in list_close_receipts(db, task_id)
    )


def _requested_task(tasks: LocalTaskManager, bound_id: Any, requested_ref: Any) -> Task | None:
    if not isinstance(bound_id, str) or not isinstance(requested_ref, str):
        return None
    try:
        task = tasks.get_task(bound_id)
        if tasks.resolve_task_reference(requested_ref, task.project_id) == task.id:
            return task
    except (ValueError, TaskNotFoundError):
        pass
    return None


def candidate_landing_approved(
    sessions: SessionManager | None,
    task_id: Any,
    sha: Any,
    requested_task: Any,
    requested_sha: Any,
) -> bool:
    """The landing call must name the approved intake unit, including its full SHA."""
    if sessions is None or not isinstance(sha, str) or requested_sha != sha:
        return False
    task = _requested_task(LocalTaskManager(sessions.db), task_id, requested_task)
    return task is not None and _approved(sessions.db, task.id, sha)


def _same_task(db: HubDatabase, ref: Any, event: CandidateEvent) -> bool:
    if not isinstance(ref, str):
        return False
    try:
        return (
            LocalTaskManager(db).resolve_task_reference(ref, event.task.project_id) == event.task.id
        )
    except (ValueError, TaskNotFoundError):
        return False


def _linked_candidate_matches(db: HubDatabase, session_id: str, event: CandidateEvent) -> bool:
    """Verify full and linked short IDs as commit objects in the task's checkout."""
    tasks = LocalTaskManager(db)
    owner = SessionManager(db).get(session_id)
    if owner is None or event.task.claimed_by_session_id != owner.id:
        return False
    artifacts = tasks.artifacts.get_artifacts(event.task.id)
    try:
        repo_path = resolve_task_repo_path(
            task_manager=tasks,
            project_manager=LocalProjectManager(db),
            task=event.task,
            project_path=artifacts.worktree_path or artifacts.clone_path or owner.workspace_path,
            machine_id=owner.machine_id,
        )
    except ValueError:
        return False
    if repo_path is None:
        return False
    identity = normalize_commit_sha(event.sha, cwd=repo_path)
    if identity is None:
        return False
    return any(
        normalize_commit_sha(linked, cwd=repo_path) == identity
        for linked in event.task.commits or []
    )


def _clear_candidate() -> dict[str, Any]:
    return {
        "candidate_received": False,
        "verdict_ready": False,
        "candidate_task": None,
        "candidate_sha": None,
        "candidate_owner": None,
        "candidate_received_at": None,
    }


def _monitor_message(
    db: HubDatabase,
    role: str,
    step: str | None,
    state: dict[str, Any],
    recipient: str,
    content: str,
    sent_at: str,
) -> dict[str, Any]:
    if not (content.startswith("Systems nominal") or content.startswith("EVENT=ALARM ")):
        return {}
    recipient_role = SessionVariableManager(db).get_variable_subset(recipient, ("_agent_type",))
    target_role = recipient_role.get("_agent_type")
    if role == "log-monitor":
        if target_role != "orchestrator":
            return {}
        return {"tick_done": False, "tick_window": sent_at}
    if target_role not in {"orchestrator", "log-monitor"}:
        return {}
    if step == "report" and content != state.get("_window_report"):
        return {}
    orchestrator = state.get("orchestrator_reported", False) if step == "report" else False
    log_monitor = state.get("log_monitor_reported", False) if step == "report" else False
    orchestrator = orchestrator or target_role == "orchestrator"
    log_monitor = log_monitor or target_role == "log-monitor"
    changes = {
        "tick_window": state.get("tick_window") if step == "report" else sent_at,
        "_window_report": content,
        "tick_done": not (orchestrator and log_monitor),
        "orchestrator_reported": orchestrator,
        "log_monitor_reported": log_monitor,
    }
    changes["orchestrator_session" if target_role == "orchestrator" else "log_monitor_session"] = (
        recipient
    )
    return changes


def _reviewer_message(
    db: HubDatabase, session_id: str, variables: dict[str, Any], event: CandidateEvent
) -> dict[str, Any]:
    fields = event.fields
    incoming = event.message.to_session == session_id
    same_task = _same_task(db, variables.get("candidate_task"), event)
    same_unit = same_task and event.sha == variables.get("candidate_sha")
    kind = fields.get("EVENT")
    parked = list(variables.get("parked_candidates") or [])
    if incoming and kind == "CANDIDATE":
        if event.message.from_session != event.task.claimed_by_session_id:
            return {}
        if variables.get("candidate_received") and not same_task:
            return {}  # Queued work cannot replace the active task.
        received_at = event.message.sent_at.isoformat()
        previous = variables.get("candidate_received_at")
        if same_task and isinstance(previous, str) and received_at <= previous:
            return {}
        if same_unit:
            return {"candidate_received_at": received_at}
        return {
            "candidate_received": True,
            "verdict_ready": False,
            "candidate_task": event.task.id,
            "candidate_sha": event.sha,
            "candidate_owner": event.message.from_session,
            "candidate_received_at": received_at,
            "parked_candidates": [
                record for record in parked if not _same_task(db, record.get("task"), event)
            ],
        }
    if kind == "CANDIDATE_WITHDRAWN" and is_task_coordinator(db, event, session_id):
        changes = _clear_candidate() if same_unit else {}
        remaining = [
            record
            for record in parked
            if not (_same_task(db, record.get("task"), event) and record.get("sha") == event.sha)
        ]
        if remaining != parked:
            changes["parked_candidates"] = remaining
        return changes
    if kind == "CANDIDATE_RESUMED" and is_task_coordinator(db, event, session_id):
        matching = [
            record
            for record in parked
            if _same_task(db, record.get("task"), event) and record.get("sha") == event.sha
        ]
        if variables.get("candidate_received") or not matching:
            return {}
        return {
            "candidate_received": True,
            "verdict_ready": _approved(db, event.task.id, event.sha),
            "candidate_task": event.task.id,
            "candidate_sha": event.sha,
            "candidate_owner": event.task.claimed_by_session_id,
            "candidate_received_at": event.message.sent_at.isoformat(),
            "parked_candidates": [record for record in parked if record not in matching],
        }
    if not same_unit:
        return {}
    if incoming and not is_task_reviewer(db, event):
        return {}
    if kind == "CANDIDATE_PARKED" and fields.get("REASON") in {
        "approval",
        "base_update",
        "review_only",
    }:
        return {
            **_clear_candidate(),
            "parked_candidates": parked + [{"task": event.task.id, "sha": event.sha}],
        }
    if kind == "CANDIDATE_VERDICT" and fields.get("VERDICT") == "BOUNCE":
        return _clear_candidate()
    if not incoming and kind in {"LANDED", "ALREADY_LANDED", "REVIEW_COMPLETE"}:
        if kind == "REVIEW_COMPLETE" and (
            fields.get("VERDICT") != "LAND" or fields.get("LANDING") != "NOT_REQUIRED"
        ):
            return {}
        if _approved(db, event.task.id, event.sha):
            return _clear_candidate()
    return {}


def advance_seat(
    db: HubDatabase,
    session_id: str,
    instance: AgentStepInstance,
    mcp_tool: tuple[str, str],
    variables: dict[str, Any],
    tool_input: dict[str, Any],
    tool_output: Any,
) -> dict[str, Any]:
    """Compute only fixed, role-specific changes from a successful completion."""
    role = instance.agent_name
    step = instance.current_step
    state = {**variables, **instance.variables}
    tasks = LocalTaskManager(db)
    if (
        role == "merge-worker"
        and step == "cleanup"
        and not state.get("worktree_id")
        and mcp_tool == ("gobby-agents", "send_message")
    ):
        messages = completion_messages(db, session_id, mcp_tool[1], tool_input, tool_output)
        if any(
            message.content.startswith("EVENT=CLONE_CLEANUP_DELEGATED ") for message in messages
        ):
            return {"worktree_cleanup_done": True}
    if role == "developer" and step == "route_skills" and mcp_tool == ("gobby-tasks", "get_task"):
        task = _requested_task(tasks, state.get("assigned_task_id"), tool_input.get("task_id"))
        if task is None or task.claimed_by_session_id != session_id:
            return {}
        skills = build_claimed_task_extra_skill_state({"claimed_tasks": {task.id: {}}}, tasks)
        return {"additional_skills": skills["claimed_task_extra_skills"], "skills_routed": True}
    if role == "code-reviewer" and mcp_tool in {
        ("gobby-tasks", "get_task"),
        ("gobby-tasks", "record_close_receipt"),
    }:
        task_id, sha = state.get("candidate_task"), state.get("candidate_sha")
        task = _requested_task(tasks, task_id, tool_input.get("task_id"))
        if task is not None and isinstance(sha, str) and state.get("candidate_received"):
            if _approved(db, task.id, sha):
                return {"verdict_ready": True}
        return {}
    if mcp_tool[0] != "gobby-agents":
        return {}
    consumed = list(state.get("_seat_consumed_messages") or [])
    for message in completion_messages(db, session_id, mcp_tool[1], tool_input, tool_output):
        if message.id in consumed:
            continue
        event = candidate_event(db, message)
        changes: dict[str, Any] = {}
        if role == "code-reviewer" and event is not None:
            changes = _reviewer_message(db, session_id, state, event)
        elif role in {"log-monitor", "metrics-monitor"} and message.from_session == session_id:
            changes = _monitor_message(
                db,
                role,
                step,
                state,
                message.to_session,
                message.content,
                message.sent_at.isoformat(),
            )
        elif role == "plan-adversary" and message.from_session == session_id:
            match = re.match(
                r"EVENT=(CONSENSUS|CONSENSUS_WITHDRAWN) SHA=([0-9a-f]{40})(?: |$)", message.content
            )
            if match and step == "review" and match[1] == "CONSENSUS":
                changes = {"consensus": True, "consensus_sha": match[2], "bounced": False}
            elif match and step == "stamp" and match[1] == "CONSENSUS_WITHDRAWN":
                if match[2] == state.get("consensus_sha"):
                    changes = {"bounced": True, "consensus": False}
        elif role == "developer" and step == "submit" and event is not None:
            if (
                message.to_session == session_id
                and _same_task(db, state.get("assigned_task_id"), event)
                and event.fields.get("EVENT") == "CANDIDATE_VERDICT"
                and event.fields.get("VERDICT") == "BOUNCE"
                and is_task_reviewer(db, event)
                and _linked_candidate_matches(db, session_id, event)
            ):
                changes = {"bounced": True, "implementation_complete": False}
        if changes:
            return {**changes, "_seat_consumed_messages": consumed + [message.id]}
    return {}
