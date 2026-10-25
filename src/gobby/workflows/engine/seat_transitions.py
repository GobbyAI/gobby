"""Fixed standing-seat effects replacing native flag writes.

Only the workflow engine dispatches these effects after successful existing
tools. Each role owns a bounded set of flags; no tool exposes this writer.
"""

from __future__ import annotations

import re
from typing import Any

from gobby.mcp_proxy.tools.task_repo_paths import resolve_task_repo_path
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessage
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.tasks.lifecycle_access import session_grants
from gobby.tasks.step_records import landing_review_approval, list_task_step_records
from gobby.utils.git import normalize_commit_sha
from gobby.workflows.claimed_task_extra_skills import build_claimed_task_extra_skill_state
from gobby.workflows.engine.seat_messages import (
    CandidateEvent,
    CandidateMessageError,
    candidate_event,
    completion_messages,
    is_task_coordinator,
    is_task_reviewer,
)
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager


def _approved(db: HubDatabase, reviewer_session_id: str, task: Task, sha: str) -> bool:
    """Landing reads the same approval, so the land gate and land_commit agree."""
    records = list_task_step_records(db, task.id)
    return landing_review_approval(task, records, sha, reviewer_session_id) is not None


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
        if target_role != "triage":
            return {}
        return {"tick_done": False, "tick_window": sent_at}
    if target_role not in {"triage", "log-monitor"}:
        return {}
    if step == "report" and content != state.get("_window_report"):
        return {}
    triage = state.get("triage_reported", False) if step == "report" else False
    log_monitor = state.get("log_monitor_reported", False) if step == "report" else False
    triage = triage or target_role == "triage"
    log_monitor = log_monitor or target_role == "log-monitor"
    changes = {
        "tick_window": state.get("tick_window") if step == "report" else sent_at,
        "_window_report": content,
        "tick_done": not (triage and log_monitor),
        "triage_reported": triage,
        "log_monitor_reported": log_monitor,
    }
    changes["triage_session" if target_role == "triage" else "log_monitor_session"] = recipient
    return changes


def _candidate_rejection(
    db: HubDatabase, variables: dict[str, Any], event: CandidateEvent
) -> str | None:
    if event.message.id in (variables.get("_seat_consumed_messages") or []):
        return "message already consumed"
    if event.message.from_session != event.task.claimed_by_session_id:
        return "sender does not claim task"
    same_task = _same_task(db, variables.get("candidate_task"), event)
    if variables.get("candidate_received") and not same_task:
        return "another candidate task is active"
    previous = variables.get("candidate_received_at")
    if same_task and isinstance(previous, str) and event.message.sent_at.isoformat() <= previous:
        return "candidate is older than or equal to the active message"
    return None


def candidate_binding_error(
    db: HubDatabase, session_id: str, message: InterSessionMessage
) -> str | None:
    """Explain incoming reviewer intake without changing protected seat state."""
    if message.to_session != session_id or message.from_session == session_id:
        return None
    reason = None
    try:
        event = candidate_event(db, message)
    except CandidateMessageError as exc:
        event = None
        reason = str(exc)
    if reason is None and (event is None or event.fields.get("EVENT") != "CANDIDATE"):
        return None
    instance = AgentStepInstanceManager(db).get_for_session(session_id)
    if instance is None or not instance.enabled or not session_grants(db, session_id, "review"):
        return None
    if reason is None and event is not None:
        state = {**SessionVariableManager(db).get_variables(session_id), **instance.variables}
        reason = _candidate_rejection(db, state, event)
    return f"CANDIDATE not bound: {reason}" if reason else None


def _reviewer_message(
    db: HubDatabase,
    session_id: str,
    variables: dict[str, Any],
    event: CandidateEvent,
    session_variables: dict[str, Any],
) -> dict[str, Any]:
    fields = event.fields
    incoming = event.message.to_session == session_id
    same_task = _same_task(db, variables.get("candidate_task"), event)
    same_unit = same_task and event.sha == variables.get("candidate_sha")
    kind = fields.get("EVENT")
    stale_session_unit = (
        not variables.get("candidate_received")
        and not variables.get("candidate_task")
        and not variables.get("candidate_sha")
        and _same_task(db, session_variables.get("candidate_task"), event)
        and event.sha == session_variables.get("candidate_sha")
    )
    if incoming and kind == "CANDIDATE":
        if _candidate_rejection(db, variables, event) is not None:
            return {}
        received_at = event.message.sent_at.isoformat()
        if same_unit:
            return {"candidate_received_at": received_at}
        return {
            "candidate_received": True,
            "verdict_ready": False,
            "candidate_task": event.task.id,
            "candidate_sha": event.sha,
            "candidate_owner": event.message.from_session,
            "candidate_received_at": received_at,
        }
    if kind == "CANDIDATE_WITHDRAWN" and is_task_coordinator(db, event, session_id):
        return _clear_candidate() if same_unit or stale_session_unit else {}
    if not same_unit:
        return {}
    if incoming and not is_task_reviewer(db, event):
        return {}
    if kind != "CANDIDATE_VERDICT":
        return {}
    # A peer's BOUNCE leaves the unit open; only this seat's own verdict ends it.
    if fields.get("VERDICT") == "BOUNCE" and event.message.from_session == session_id:
        return _clear_candidate()
    # The Dev Manager lands an approved LAND, so the reviewer's unit ends at the verdict.
    if (
        not incoming
        and fields.get("VERDICT") == "LAND"
        and _approved(db, session_id, event.task, event.sha)
    ):
        return _clear_candidate()
    return {}


def _returns_to_implement(fields: dict[str, str]) -> bool:
    """A pre-land BOUNCE or PROOF_REQUEST for evidence."""
    if fields.get("EVENT") == "PROOF_REQUEST":
        return True
    return fields.get("EVENT") == "CANDIDATE_VERDICT" and fields.get("VERDICT") == "BOUNCE"


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
        step == "route_skills"
        and mcp_tool == ("gobby-tasks", "get_task")
        and session_grants(db, session_id, "implementation")
    ):
        task = _requested_task(tasks, state.get("assigned_task_id"), tool_input.get("task_id"))
        if task is None or task.claimed_by_session_id != session_id:
            return {}
        skills = build_claimed_task_extra_skill_state({"claimed_tasks": {task.id: {}}}, tasks)
        return {"additional_skills": skills["claimed_task_extra_skills"], "skills_routed": True}
    if (
        mcp_tool in {("gobby-tasks", "get_task"), ("gobby-tasks", "record_review_step")}
        and session_grants(db, session_id, "review")
    ):
        task_id, sha = state.get("candidate_task"), state.get("candidate_sha")
        task = _requested_task(tasks, task_id, tool_input.get("task_id"))
        if task is not None and isinstance(sha, str) and state.get("candidate_received"):
            if _approved(db, session_id, task, sha):
                return {"verdict_ready": True}
        return {}
    if mcp_tool[0] != "gobby-agents":
        return {}
    consumed = list(state.get("_seat_consumed_messages") or [])
    for message in completion_messages(db, session_id, mcp_tool[1], tool_input, tool_output):
        if message.id in consumed:
            continue
        try:
            event = candidate_event(db, message)
        except CandidateMessageError:
            continue
        changes: dict[str, Any] = {}
        if event is not None and session_grants(db, session_id, "review"):
            changes = _reviewer_message(db, session_id, state, event, variables)
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
                r"EVENT=(CONSENSUS|CONSENSUS_WITHDRAWN) SHA=([0-9a-f]{40})(?:\s|$)", message.content
            )
            if match and step == "review" and match[1] == "CONSENSUS":
                changes = {"consensus": True, "consensus_sha": match[2], "bounced": False}
            elif match and step == "stamp" and match[1] == "CONSENSUS_WITHDRAWN":
                if not state.get("consensus_sha") or match[2] == state.get("consensus_sha"):
                    changes = {"bounced": True, "consensus": False}
        elif (
            step == "submit"
            and event is not None
            and session_grants(db, session_id, "implementation")
        ):
            if (
                message.to_session == session_id
                and _same_task(db, state.get("assigned_task_id"), event)
                and _returns_to_implement(event.fields)
                and is_task_reviewer(db, event)
                and _linked_candidate_matches(db, session_id, event)
            ):
                # The flag returns the claim to implement with shell access; link_commit clears it.
                changes = {"bounced": True, "implementation_complete": False}
        if changes:
            return {**changes, "_seat_consumed_messages": (consumed + [message.id])[-64:]}
    return {}
