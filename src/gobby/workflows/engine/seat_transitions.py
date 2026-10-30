"""Definition-authorized workflow effects replacing native flag writes.

Only the workflow engine dispatches these effects after successful existing
tools. Each declared permission owns bounded flags; no tool exposes this writer.
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
from gobby.tasks.step_records import (
    approved_task_step,
    landing_review_approval,
    list_task_step_records,
)
from gobby.utils.git import normalize_commit_sha
from gobby.workflows.agent_resolver import session_agent_definition
from gobby.workflows.claimed_task_extra_skills import build_claimed_task_extra_skill_state
from gobby.workflows.engine.seat_messages import (
    CandidateEvent,
    CandidateMessageError,
    candidate_event,
    completion_messages,
    is_relaying_dev_manager,
    is_task_coordinator,
    is_task_reviewer,
)
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager


def _approved(db: HubDatabase, reviewer_session_id: str, task: Task, sha: str) -> bool:
    """Landing reads the same approval, so the land gate and land_commit agree."""
    records = list_task_step_records(db, task.id)
    return landing_review_approval(task, records, sha, reviewer_session_id) is not None


def _no_work_approved(
    db: HubDatabase, reviewer_session_id: str, task: Task, disposition: str
) -> bool:
    """The reviewer's approved no-work review of a task that has no linked commit."""
    current = approved_task_step(task, list_task_step_records(db, task.id), "review")
    return (
        current is not None
        and current.commit_sha is None
        and current.disposition == disposition
        and current.session_id == reviewer_session_id
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


def _same_task(db: HubDatabase, ref: Any, event: CandidateEvent) -> bool:
    if not isinstance(ref, str):
        return False
    try:
        return (
            LocalTaskManager(db).resolve_task_reference(ref, event.task.project_id) == event.task.id
        )
    except (ValueError, TaskNotFoundError):
        return False


def _coordinates_holder(db: HubDatabase, session_id: str, event: CandidateEvent) -> bool:
    """The root Orchestrator, or the task's bound Dev Manager when this seat is in its team."""
    from gobby.mcp_proxy.tools.tasks._claim_release import _can_release
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext

    sessions = SessionManager(db)
    caller = sessions.get(event.message.from_session)
    holder = sessions.get(session_id)
    if caller is None or holder is None or caller.id == holder.id:
        return False
    return _can_release(
        RegistryContext(task_manager=LocalTaskManager(db)), caller, holder, event.task.id
    )


def _submit_return_rejection(
    db: HubDatabase, session_id: str, state: dict[str, Any], event: CandidateEvent
) -> str | None:
    """Why reading ``event`` leaves a submitted developer in submit; None returns it."""
    proof = event.fields.get("EVENT") == "PROOF_REQUEST"
    if event.message.to_session != session_id:
        return "message is not addressed to this seat"
    if event.message.id in (state.get("_seat_consumed_messages") or []):
        return "message already consumed"
    if not _returns_to_implement(event.fields):
        return "only EVENT=PROOF_REQUEST or CANDIDATE_VERDICT VERDICT=BOUNCE returns to implement"
    if not _same_task(db, state.get("assigned_task_id"), event):
        return "TASK is not this seat's assigned task"
    # A proof request asks for evidence, so the claimant's coordinators may send it too.
    if not is_task_reviewer(db, event) and not (
        proof and _coordinates_holder(db, session_id, event)
    ):
        if proof:
            return "sender is not the task's reviewer, bound Dev Manager or Orchestrator"
        return "sender is not the task's reviewer"
    if event.task.claimed_by_session_id != session_id:
        return "this seat does not claim the task"
    if not _commit_linked_to_claimant(db, event):
        return "SHA is not a commit linked to the task"
    return None


def _commit_linked_to_claimant(db: HubDatabase, event: CandidateEvent) -> bool:
    """Verify full and linked short IDs as commit objects in the claimant's checkout."""
    tasks = LocalTaskManager(db)
    claimant_id = event.task.claimed_by_session_id
    owner = SessionManager(db).get(claimant_id) if claimant_id else None
    if owner is None:
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
        "candidate_disposition": None,
        "candidate_owner": None,
        "candidate_received_at": None,
    }


def _monitor_message(
    db: HubDatabase,
    required_recipients: list[str],
    step: str | None,
    state: dict[str, Any],
    recipient: str,
    content: str,
    sent_at: str,
) -> dict[str, Any]:
    if not (content.startswith("Systems nominal") or content.startswith("EVENT=ALARM ")):
        return {}
    recipient_session = SessionManager(db).get(recipient)
    if recipient_session is None:
        return {}
    definition = session_agent_definition(db, recipient_session)
    if definition is None or definition[0].name not in required_recipients:
        return {}
    target = definition[0].name
    if step == "report" and content != state.get("_window_report"):
        return {}
    reported = set(state.get("_reported_to") or []) if step == "report" else set()
    reported.add(target)
    return {
        "tick_window": state.get("tick_window") if step == "report" else sent_at,
        "_window_report": content,
        "tick_done": not set(required_recipients).issubset(reported),
        "_reported_to": sorted(reported),
    }


def _candidate_rejection(
    db: HubDatabase, variables: dict[str, Any], event: CandidateEvent, reviewer_session_id: str
) -> str | None:
    if event.message.id in (variables.get("_seat_consumed_messages") or []):
        return "message already consumed"
    relayed = event.message.from_session != event.task.claimed_by_session_id
    # A relay binds only from the bound Dev Manager, for a SHA the claimant linked.
    if relayed and not is_relaying_dev_manager(db, event, reviewer_session_id):
        return "sender does not claim task"
    if event.sha is None:
        # A no-work unit binds only a task with no linked work, whoever sends it.
        if event.task.commits:
            return "DISPOSITION needs a task with no linked commits"
    elif relayed and not _commit_linked_to_claimant(db, event):
        return "COMMIT is not linked to the claimant's task"
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
    """Explain refused reviewer intake or submit return without changing protected seat state."""
    if message.to_session != session_id or message.from_session == session_id:
        return None
    reason = None
    try:
        event = candidate_event(db, message)
    except CandidateMessageError as exc:
        event = None
        reason = str(exc)
    instance = AgentStepInstanceManager(db).get_for_session(session_id)
    if instance is None or not instance.enabled:
        return None
    if (
        event is not None
        and instance.current_step == "submit"
        and _returns_to_implement(event.fields)
        and session_grants(db, session_id, "implementation")
    ):
        state = {**SessionVariableManager(db).get_variables(session_id), **instance.variables}
        why = _submit_return_rejection(db, session_id, state, event)
        return f"{event.fields['EVENT']} did not return submit to implement: {why}" if why else None
    if reason is None and (event is None or event.fields.get("EVENT") != "CANDIDATE"):
        return None
    if not session_grants(db, session_id, "review"):
        return None
    if reason is None and event is not None:
        state = {**SessionVariableManager(db).get_variables(session_id), **instance.variables}
        reason = _candidate_rejection(db, state, event, session_id)
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
    # A unit is its task and its commit, or its task and no-work disposition. A no-work unit
    # holds only while its task has no linked commit; linked work leaves nothing to clear.
    disposition = fields.get("DISPOSITION")
    open_unit = disposition is None or not event.task.commits
    same_unit = (
        same_task
        and open_unit
        and event.sha == variables.get("candidate_sha")
        and disposition == variables.get("candidate_disposition")
    )
    kind = fields.get("EVENT")
    stale_session_unit = (
        not variables.get("candidate_received")
        and not variables.get("candidate_task")
        and not variables.get("candidate_sha")
        and open_unit
        and _same_task(db, session_variables.get("candidate_task"), event)
        and event.sha == session_variables.get("candidate_sha")
        and disposition == session_variables.get("candidate_disposition")
    )
    if incoming and kind == "CANDIDATE":
        if _candidate_rejection(db, variables, event, session_id) is not None:
            return {}
        received_at = event.message.sent_at.isoformat()
        if same_unit:
            return {"candidate_received_at": received_at}
        return {
            "candidate_received": True,
            "verdict_ready": False,
            "candidate_task": event.task.id,
            "candidate_sha": event.sha,
            "candidate_disposition": disposition,
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
    # A no-work unit has no commit to land, so only a commit unit takes a LAND verdict.
    if (
        not incoming
        and fields.get("VERDICT") == "LAND"
        and event.sha is not None
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
    """Compute only effects authorized by the caller's enabled definition."""
    session = SessionManager(db).get(session_id)
    if session is None:
        return {}
    definition = session_agent_definition(db, session)
    if definition is None:
        return {}
    body = definition[0]
    step = instance.current_step
    state = {**variables, **instance.variables}
    tasks = LocalTaskManager(db)
    if (
        step == "route_skills"
        and mcp_tool == ("gobby-tasks", "get_task")
        and "implementation" in body.task_step_writes
    ):
        task = _requested_task(tasks, state.get("assigned_task_id"), tool_input.get("task_id"))
        if task is None or task.claimed_by_session_id != session_id:
            return {}
        skills = build_claimed_task_extra_skill_state({"claimed_tasks": {task.id: {}}}, tasks)
        return {"additional_skills": skills["claimed_task_extra_skills"], "skills_routed": True}
    if mcp_tool in {("gobby-tasks", "get_task"), ("gobby-tasks", "record_review_step")} and (
        "review" in body.task_step_writes
    ):
        task_id, sha = state.get("candidate_task"), state.get("candidate_sha")
        disposition = state.get("candidate_disposition")
        task = _requested_task(tasks, task_id, tool_input.get("task_id"))
        if task is not None and state.get("candidate_received"):
            if isinstance(sha, str) and _approved(db, session_id, task, sha):
                return {"verdict_ready": True}
            if isinstance(disposition, str) and _no_work_approved(
                db, session_id, task, disposition
            ):
                # A no-work unit has no commit to land, so the approved review closes it.
                return _clear_candidate()
        return {}
    if mcp_tool[0] != "gobby-agents":
        return {}
    consumed = list(state.get("_seat_consumed_messages") or [])
    monitor_updates: dict[str, Any] = {}
    for message in completion_messages(db, session_id, mcp_tool[1], tool_input, tool_output):
        if message.id in consumed:
            continue
        try:
            event = candidate_event(db, message)
        except CandidateMessageError:
            continue
        changes: dict[str, Any] = {}
        if (
            event is not None
            and step == "submit"
            and "implementation" in body.task_step_writes
            and _returns_to_implement(event.fields)
        ):
            if _submit_return_rejection(db, session_id, state, event) is None:
                # Return the claim to implement with shell access; link_commit clears the flag.
                changes = {"bounced": True, "implementation_complete": False}
        elif event is not None and "review" in body.task_step_writes:
            changes = _reviewer_message(db, session_id, state, event, variables)
        elif (
            body.monitor_report_recipients
            and message.from_session == session_id
            and message.content.startswith(("Systems nominal", "EVENT=ALARM "))
        ):
            changes = _monitor_message(
                db,
                body.monitor_report_recipients,
                step,
                state,
                message.to_session,
                message.content,
                message.sent_at.isoformat(),
            )
            if changes:
                monitor_updates.update(changes)
                state.update(changes)
                # A broadcast's recipients share one window even before the step transition.
                step = "report"
                consumed.append(message.id)
            continue
        elif "track_plan_consensus" in body.capabilities and message.from_session == session_id:
            match = re.match(
                r"EVENT=(CONSENSUS|CONSENSUS_WITHDRAWN) SHA=([0-9a-f]{40})(?:\s|$)",
                message.content.partition("\n")[0],
            )
            if match and step == "review" and match[1] == "CONSENSUS":
                changes = {"consensus": True, "consensus_sha": match[2], "bounced": False}
            elif match and step == "stamp" and match[1] == "CONSENSUS_WITHDRAWN":
                if not state.get("consensus_sha") or match[2] == state.get("consensus_sha"):
                    changes = {"bounced": True, "consensus": False}
        if changes:
            return {
                **monitor_updates,
                **changes,
                "_seat_consumed_messages": (consumed + [message.id])[-64:],
            }
    return {**monitor_updates, "_seat_consumed_messages": consumed[-64:]} if monitor_updates else {}
