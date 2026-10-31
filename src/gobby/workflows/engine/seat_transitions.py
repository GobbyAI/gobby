"""Definition-authorized workflow effects replacing native flag writes.

Only the workflow engine dispatches these effects after successful existing
tools. Each declared permission owns bounded flags; no tool exposes this writer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.task_repo_paths import resolve_task_repo_path
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessage
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import (
    LocalTaskManager,
    Task,
    TaskAlreadyClaimedError,
    TaskClosedError,
    TaskNotFoundError,
)
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.tasks.lifecycle_access import session_grants
from gobby.tasks.step_records import (
    approved_task_step,
    landing_review_approval,
    list_task_step_records,
)
from gobby.utils.git import normalize_commit_sha, run_git_command
from gobby.workflows.agent_resolver import session_agent_definition, session_has_capability
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
from gobby.workflows.step_binding import BOUND_REVIEW_EPOCH_VARIABLE
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager
from gobby.workflows.task_claim_state import task_edited_file_set

# Owned by the step instance. The engine must not mirror it into session variables: a
# mirror outlives a lost instance write and refuses the same BOUNCE on its next read.
SEAT_CONSUMED_KEY = "_seat_consumed_messages"


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
    if event.message.id in (state.get(SEAT_CONSUMED_KEY) or []):
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


def _claimant_at(db: HubDatabase, event: CandidateEvent) -> str | None:
    """The session that claimed the task when the message was sent.

    A hand-off moves the claim after the sender speaks, so a message binds to the claimant it was
    sent under. The current claimant is the fallback when no claim predates the message.
    """
    for row in SessionTaskManager(db).get_task_sessions(event.task.id):  # newest first
        if row["action"] == "claimed" and row["created_at"] <= event.message.sent_at:
            return str(row["session_id"])
    return event.task.claimed_by_session_id


def _candidate_rejection(
    db: HubDatabase, variables: dict[str, Any], event: CandidateEvent, reviewer_session_id: str
) -> str | None:
    if event.message.id in (variables.get(SEAT_CONSUMED_KEY) or []):
        return "message already consumed"
    # A teammate's candidate that moved the claim to this reader is its own unit, not a relay.
    handed = event.task.claimed_by_session_id == reviewer_session_id and _same_team(
        db, event.message.from_session, reviewer_session_id
    )
    relayed = event.message.from_session != _claimant_at(db, event) and not handed
    # A relay binds only from the bound Dev Manager, for a SHA the claimant linked. A task with
    # no claim has no claimant to link against, so only a base-merge author binds there.
    claim_free = event.task.claimed_by_session_id is None
    if relayed and (claim_free or not is_relaying_dev_manager(db, event, reviewer_session_id)):
        error = _base_merge_rejection(db, event)
        if error is not None:
            return error
    elif event.sha is None:
        # A no-work unit binds only a task with no linked work, whoever sends it.
        if event.task.commits:
            return "DISPOSITION needs a task with no linked commits"
    elif relayed and not _commit_linked_to_claimant(db, event):
        return "COMMIT is not linked to the claimant's task"
    elif handed and event.sha not in (event.task.commits or []):
        return "COMMIT is not linked to the task"
    same_task = _same_task(db, variables.get("candidate_task"), event)
    if variables.get("candidate_received") and not same_task:
        return "another candidate task is active"
    previous = variables.get("candidate_received_at")
    if same_task and isinstance(previous, str) and event.message.sent_at.isoformat() <= previous:
        return "candidate is older than or equal to the active message"
    return None


def _base_merge_rejection(db: HubDatabase, event: CandidateEvent) -> str | None:
    """Bind a base merge its capable sender authored, at the task's latest linked commit.

    The sender needs the base-merge capability, and any claim authority it declares must cover
    this task. The COMMIT must be the latest link, a merge this sender made, and a merge of an
    admitted tip: the integration branch or the epic's branch.
    """
    from gobby.workflows.base_merge_commits import CAPABILITY, admitted_parent_tips
    from gobby.workflows.reserved_variables import AUTHORED_MERGES_VARIABLE

    refused = "sender does not claim task"
    sender = SessionManager(db).get(event.message.from_session)
    if sender is None or not session_has_capability(db, sender.id, CAPABILITY):
        return refused
    if not _claim_authority_covers(db, sender, event.task):
        return refused
    worktree = LocalWorktreeManager(db).get_by_task(event.task.id)
    if worktree is None or event.sha is None or not event.task.commits:
        return "COMMIT is not the task's latest linked commit"
    repo = Path(worktree.worktree_path)
    sha = _full_commit(event.sha, repo)
    if sha is None or sha != _full_commit(event.task.commits[-1], repo):
        return "COMMIT is not the task's latest linked commit"
    authored = SessionVariableManager(db).get_variables(sender.id).get(AUTHORED_MERGES_VARIABLE)
    recorded = authored if isinstance(authored, list) else []
    if sha not in {_full_commit(entry, repo) for entry in recorded if isinstance(entry, str)}:
        return "COMMIT was not authored by the sender"
    second = _full_commit(f"{sha}^2", repo)
    tips = admitted_parent_tips(db, event.task, repo)
    if second is None or tips is None or second not in tips:
        return "COMMIT is not a base merge of the integration or epic branch"
    return None


def _claim_authority_covers(db: HubDatabase, sender: Session, task: Task) -> bool:
    """A sender that declares claim authority must hold it for this task; others need none.

    A declared manager covers a task only as a seat of that task's team, so an unbound or
    other-team manager is refused while a root coordinator with the capability is not.
    """
    from gobby.agents.team_lifecycle import seat_team, task_team
    from gobby.tasks.lifecycle_access import is_operator_coordinator

    definition = session_agent_definition(db, sender)
    if definition is None:
        return False
    if "manage_task_claims" not in definition[0].capabilities:
        return True
    if is_operator_coordinator(LocalTaskManager(db), sender, capability="manage_task_claims"):
        return True
    manager = seat_team(db, sender.id)
    team = task_team(db, task.id)
    return (
        manager is not None
        and team is not None
        and (team.team_id == manager.team_id or team.parent_team_id == manager.team_id)
    )


def _full_commit(ref: str, repo: Path) -> str | None:
    """The full SHA a ref names in the repo, or None when it names no commit."""
    return run_git_command(["git", "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], repo)


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


def _same_team(db: HubDatabase, session_id: str, other_id: str) -> bool:
    """Whether both seats are seats of one team's runbook execution."""
    from gobby.agents.team_lifecycle import seat_team
    from gobby.mcp_proxy.tools.tasks._claim_release import _is_team_seat

    team = seat_team(db, session_id)
    return team is not None and _is_team_seat(db, other_id, team)


def _move_claim(
    db: HubDatabase,
    task_id: str,
    holder_id: str,
    recipient_id: str,
    variables: dict[str, Any],
    *,
    handed: bool,
) -> bool:
    """Move a claim from its holder; uncommitted attributed edits keep it where it is."""
    if task_edited_file_set(variables, task_id):
        return False
    try:
        LocalTaskManager(db).claim_task_for_agent(
            task_id, session_id=recipient_id, expected_owner=holder_id, handed=handed
        )
    except (TaskAlreadyClaimedError, TaskClosedError):
        return False
    return True


def _claim_to_reviewer(
    db: HubDatabase, session_id: str, event: CandidateEvent, variables: dict[str, Any]
) -> dict[str, Any]:
    """A claimant's CANDIDATE to a reviewer on its team hands the claim to that reviewer."""
    recipient = event.message.to_session
    if not recipient or not _same_team(db, session_id, recipient):
        return {}
    session = SessionManager(db).get(recipient)
    definition = session_agent_definition(db, session) if session is not None else None
    if definition is None or "review" not in definition[0].task_step_writes:
        return {}
    if not _move_claim(db, event.task.id, session_id, recipient, variables, handed=True):
        return {}
    return {"fixing": False}


def _relay_hands_claim(db: HubDatabase, reviewer_id: str, event: CandidateEvent) -> bool:
    """A reviewer reading the bound Dev Manager's relay of a fixer's CANDIDATE takes the claim.

    _candidate_rejection has already authenticated the relay: the bound Dev Manager sent it, and
    the SHA is linked to the claimant. The claimant's edit ledger guards the move, because its
    live edits belong to the claimant's session, not the reviewer's.
    """
    holder_id = event.task.claimed_by_session_id
    # A no-work unit has no commit to land, so its holder keeps the claim to record the verdict.
    if event.sha is None or holder_id is None:
        return True
    if holder_id in (reviewer_id, event.message.from_session):
        return True
    if not _same_team(db, holder_id, reviewer_id):
        return False
    holder = SessionManager(db).get(holder_id)
    definition = session_agent_definition(db, holder) if holder is not None else None
    # Only a fixer (a review-writing holder) hands its claim on; a Developer keeps it to its BOUNCE.
    if definition is None or "review" not in definition[0].task_step_writes:
        return True
    holder_variables = SessionVariableManager(db).get_variables(holder_id)
    return _move_claim(db, event.task.id, holder_id, reviewer_id, holder_variables, handed=True)


def _claim_to_closer(
    db: HubDatabase, session_id: str, event: CandidateEvent, variables: dict[str, Any]
) -> None:
    """A reviewer's LAND hands the claim to the Dev Manager that closes the unit."""
    from gobby.mcp_proxy.tools.tasks._claim_release import _can_release
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext

    sessions = SessionManager(db)
    recipient = event.message.to_session
    caller = sessions.get(recipient) if recipient else None
    holder = sessions.get(session_id)
    if caller is None or holder is None:
        return
    if _can_release(
        RegistryContext(task_manager=LocalTaskManager(db)), caller, holder, event.task.id
    ):
        _move_claim(db, event.task.id, session_id, caller.id, variables, handed=False)


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
        if not _relay_hands_claim(db, session_id, event):
            return {}
        return {
            "candidate_received": True,
            "verdict_ready": False,
            "candidate_task": event.task.id,
            "candidate_sha": event.sha,
            "candidate_disposition": disposition,
            "candidate_owner": event.message.from_session,
            "candidate_received_at": received_at,
        }
    if not incoming and kind == "CANDIDATE" and event.task.claimed_by_session_id == session_id:
        return _claim_to_reviewer(db, session_id, event, variables)
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
        if event.task.claimed_by_session_id == session_id:
            _claim_to_closer(db, session_id, event, variables)
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
    if mcp_tool == ("gobby-tasks", "claim_task") and "review" in body.task_step_writes:
        # The claimed task's review epoch, so a reset returns this seat to its await step.
        result = (tool_output.get("result") or tool_output) if isinstance(tool_output, dict) else {}
        task_id = result.get("task_id") if isinstance(result, dict) else None
        claimed = _requested_task(tasks, task_id, task_id)
        if claimed is None or claimed.claimed_by_session_id != session_id:
            return {}
        return {BOUND_REVIEW_EPOCH_VARIABLE: claimed.review_epoch}
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
    consumed = list(state.get(SEAT_CONSUMED_KEY) or [])
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
                SEAT_CONSUMED_KEY: (consumed + [message.id])[-64:],
            }
    return {**monitor_updates, SEAT_CONSUMED_KEY: consumed[-64:]} if monitor_updates else {}
