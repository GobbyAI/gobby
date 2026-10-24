"""Coordinator release of a named seat's task claim, and the Dev Manager's in-team hand-off."""

from typing import Any

from gobby.agents.team_lifecycle import seat_team
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_claim import record_claimant_state
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import (
    AgentTaskClaimMutation,
    HubDatabase,
    SessionVariableTransfer,
)
from gobby.storage.session_models import Session
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.tasks.lifecycle_access import is_operator_coordinator, session_agent_definition
from gobby.tasks.step_records import fixer_round, list_task_step_records
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.task_claim_state import task_edited_checkout_paths, task_edited_file_set
from gobby.workflows.task_dirty_state import task_dirty_paths

_STALE_HOLDER = {
    "error": "The named holder no longer owns this open task",
    "error_code": "task_claim_conflict",
}


def _can_release(ctx: RegistryContext, caller: Session, holder: Session, task_id: str) -> bool:
    """The Orchestrator, or the Dev Manager of the task's team when the holder is its seat."""
    if caller.project_id != holder.project_id or caller.machine_id != holder.machine_id:
        return False
    role = ctx.session_var_manager.get_variable_subset(caller.id, ("_agent_type",))
    if role.get("_agent_type") == "orchestrator":
        return is_operator_coordinator(ctx.task_manager, caller)
    db = ctx.task_manager.db
    team = seat_team(db, caller.id)
    return (
        team is not None
        and team.task_id == task_id
        and _bound_dev_manager(ctx, caller, task_id)
        and seat_team(db, holder.id) == team
    )


def _uncommitted_refusal(
    ctx: RegistryContext, holder: Session, task_id: str
) -> dict[str, Any] | None:
    """Refuse while the holder has dirty task-attributed files; its variables must be locked."""
    variables = ctx.session_var_manager.get_variables(holder.id)
    by_root: dict[str, set[str]] = {}
    pairs = task_edited_checkout_paths(variables, task_id)
    for root, path in pairs:
        by_root.setdefault(root, set()).add(path)
    legacy = task_edited_file_set(variables, task_id) - {path for _, path in pairs}
    if legacy:
        legacy_root = holder.workspace_path or ctx.get_project_repo_path(
            holder.project_id, holder.machine_id
        )
        if not legacy_root:
            return {"error": "Holder checkout is unresolved", "error_code": "checkout_unresolved"}
        by_root.setdefault(legacy_root, set()).update(legacy)
    dirty: list[str] = []
    for root, paths in sorted(by_root.items()):
        found = task_dirty_paths(paths, root)
        if found is None:
            return {
                "error": f"Cannot verify holder checkout {root}",
                "error_code": "checkout_unresolved",
            }
        dirty.extend(f"{root}/{path}" for path in sorted(found))
    if dirty:
        return {
            "error": "Task has uncommitted attributed files",
            "error_code": "task_has_uncommitted_changes",
            "paths": dirty,
        }
    return None


def register_release_task_claim(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    def release_task_claim(task_id: str, holder_session_ref: str, reason: str) -> dict[str, Any]:
        caller_id = get_current_session_id()
        if not caller_id:
            return {"error": "Session context is required", "error_code": "session_required"}
        if not reason.strip():
            return {"error": "A release reason is required", "error_code": "reason_required"}
        try:
            caller = ctx.session_manager.get(ctx.resolve_session_id(caller_id))
            if caller is None:
                return {"error": "Caller not found", "error_code": "session_required"}
            holder_id = ctx.session_manager.resolve_session_reference(
                holder_session_ref, caller.project_id
            )
            holder = ctx.session_manager.get(holder_id)
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
            if holder is None or not _can_release(ctx, caller, holder, resolved_id):
                return {
                    "error": "Only the Orchestrator or the holder's Dev Manager may release a claim",
                    "error_code": "coordinator_required",
                }
            db = ctx.task_manager.db
            with db.transaction() as conn:
                # Serialize ownership, file attribution and projection cleanup with
                # ordinary claims/releases. Failure of any step rolls back the release.
                row = conn.execute(
                    "SELECT claimed_by_session_id, project_id, closed_at FROM tasks "
                    "WHERE id = %s FOR UPDATE",
                    (resolved_id,),
                ).fetchone()
                if row is None or str(row["project_id"]) != caller.project_id:
                    return {"error": "Task not found", "error_code": "task_not_found"}
                if row["closed_at"] is not None or str(row["claimed_by_session_id"]) != holder.id:
                    return dict(_STALE_HOLDER)
                conn.acquire_additional_lock(SessionVariableTransfer((holder.id,)))
                refusal = _uncommitted_refusal(ctx, holder, resolved_id)
                if refusal is not None:
                    return refusal
                ctx.task_manager.release_task_claim(resolved_id)
                event = TaskLifecycleEventManager(db).record_lifecycle_event(
                    resolved_id,
                    "claim:held",
                    "claim:released",
                    f"Released claim held by {holder.id}: {reason.strip()}",
                    by_actor=caller.id,
                )
            return {
                "success": True,
                "task_id": resolved_id,
                "released_from_session_id": holder.id,
                "released_by_session_id": caller.id,
                "reason": reason.strip(),
                "event_id": event.id,
            }
        except (TaskNotFoundError, ValueError) as exc:
            return {"error": str(exc)}

    registry.register(
        name="release_task_claim",
        description=(
            "Orchestrator or the holder's Dev Manager: release a named seat's claim on an open "
            "task. "
            "Refuses uncommitted task-attributed files, records the actor and reason, "
            "and preserves unrelated task bindings."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "holder_session_ref": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["task_id", "holder_session_ref", "reason"],
        },
        func=release_task_claim,
    )
    register_hand_off_task_claim(registry, ctx)


def _bound_dev_manager(ctx: RegistryContext, caller: Session, task_id: str) -> bool:
    """The caller's active run is bound to the task and its definition writes closing."""
    db = ctx.task_manager.db
    run = LocalAgentRunManager(db).get_by_session(caller.id)
    definition = session_agent_definition(db, caller)
    return (
        run is not None
        and run.task_id == task_id
        and definition is not None
        and "closing" in definition[0].task_step_writes
    )


def _recipient_refusal(
    db: HubDatabase, caller_id: str, recipient: Session, task_id: str, epoch: int
) -> dict[str, str] | None:
    """Refuse a move that disqualifies the seat the review sequence needs next.

    A former claimant never lands or reviews the task, so the seats that close it never
    take its claim, and the epoch's reviewer takes it only at the second rejection.
    """
    definition = session_agent_definition(db, recipient)
    if recipient.id == caller_id or (
        definition is not None and "closing" in definition[0].task_step_writes
    ):
        return {
            "error": "The seat that closes the task cannot hold its claim",
            "error_code": "closer_claim_forbidden",
        }
    records = list_task_step_records(db, task_id)
    reviewed = any(
        record.step == "review" and record.epoch == epoch and record.session_id == recipient.id
        for record in records
    )
    if reviewed and not fixer_round(records, epoch):
        return {
            "error": "A reviewer of this epoch takes the claim only at the second rejection",
            "error_code": "reviewer_claim_early",
        }
    return None


def register_hand_off_task_claim(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    def hand_off_task_claim(
        task_id: str, holder_session_ref: str, recipient_session_ref: str, reason: str
    ) -> dict[str, Any]:
        caller_id = get_current_session_id()
        if not caller_id:
            return {"error": "Session context is required", "error_code": "session_required"}
        if not reason.strip():
            return {"error": "A hand-off reason is required", "error_code": "reason_required"}
        try:
            caller = ctx.session_manager.get(ctx.resolve_session_id(caller_id))
            if caller is None:
                return {"error": "Caller not found", "error_code": "session_required"}
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
            db = ctx.task_manager.db
            team = seat_team(db, caller.id)
            if (
                team is None
                or team.task_id != resolved_id
                or not _bound_dev_manager(ctx, caller, resolved_id)
            ):
                return {
                    "error": "Only the task's Dev Manager may hand off its claim",
                    "error_code": "team_manager_required",
                }
            holder, recipient = (
                ctx.session_manager.get(
                    ctx.session_manager.resolve_session_reference(ref, caller.project_id)
                )
                for ref in (holder_session_ref, recipient_session_ref)
            )
            if (
                holder is None
                or recipient is None
                or seat_team(db, holder.id) != team
                or seat_team(db, recipient.id) != team
            ):
                return {
                    "error": "The holder and the recipient must be seats of the task's team",
                    "error_code": "team_seat_required",
                }
            if holder.id == recipient.id:
                return {"error": "The recipient already holds the claim", "error_code": "same_seat"}
            # claim_task_for_agent re-takes these exact locks, so the ownership check, the
            # dirty-file check and the move commit as one transfer.
            with db.transaction_immediate(AgentTaskClaimMutation(recipient.id)) as conn:
                row = conn.execute(
                    "SELECT claimed_by_session_id, project_id, closed_at, review_epoch FROM tasks "
                    "WHERE id = %s FOR UPDATE",
                    (resolved_id,),
                ).fetchone()
                if row is None or str(row["project_id"]) != caller.project_id:
                    return {"error": "Task not found", "error_code": "task_not_found"}
                if row["closed_at"] is not None or str(row["claimed_by_session_id"]) != holder.id:
                    return dict(_STALE_HOLDER)
                refusal = _recipient_refusal(
                    db, caller.id, recipient, resolved_id, int(row["review_epoch"])
                )
                if refusal is not None:
                    return refusal
                sessions = {recipient.id}
                sessions.add(holder.id)
                conn.acquire_additional_lock(SessionVariableTransfer(tuple(sessions)))
                refusal = _uncommitted_refusal(ctx, holder, resolved_id)
                if refusal is not None:
                    return refusal
                handed = ctx.task_manager.claim_task_for_agent(
                    resolved_id, session_id=recipient.id, expected_owner=holder.id
                )
                event = TaskLifecycleEventManager(db).record_lifecycle_event(
                    resolved_id,
                    "claim:held",
                    "claim:handed_off",
                    f"Handed claim from {holder.id} to {recipient.id}: {reason.strip()}",
                    by_actor=caller.id,
                )
            record_claimant_state(ctx, recipient.id, handed)
            return {
                "success": True,
                "task_id": resolved_id,
                "handed_from_session_id": holder.id,
                "handed_to_session_id": recipient.id,
                "handed_by_session_id": caller.id,
                "reason": reason.strip(),
                "event_id": event.id,
            }
        except (TaskNotFoundError, ValueError) as exc:
            return {"error": str(exc)}

    registry.register(
        name="hand_off_task_claim",
        description=(
            "The task's Dev Manager: move the task claim from its holder to another seat of the "
            "same team. The holder must still own the task and have no uncommitted "
            "task-attributed files; the transfer is atomic and recorded with the reason. A seat "
            "that closes the task never takes its claim, and a reviewer of the current epoch "
            "takes it only at the second rejection."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "holder_session_ref": {"type": "string"},
                "recipient_session_ref": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["task_id", "holder_session_ref", "recipient_session_ref", "reason"],
        },
        func=hand_off_task_claim,
    )
