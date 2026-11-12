"""Authorized claim release and explicit transfer."""

from dataclasses import dataclass
from typing import Any

from gobby.agents.team_lifecycle import seat_team, task_team
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_claim import record_claimant_state
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.hub.protocol import (
    AgentTaskClaimMutation,
    SessionVariableTransfer,
)
from gobby.storage.session_models import Session
from gobby.storage.tasks import Task, TaskNotFoundError
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.tasks.attributed_roots import is_ignorable_attributed_root
from gobby.tasks.lifecycle_access import task_scope_refusal
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.agent_resolver import session_agent_definition
from gobby.workflows.task_claim_state import task_edited_checkout_paths, task_edited_file_set
from gobby.workflows.task_dirty_state import task_dirty_paths

_STALE_HOLDER = {
    "error": "The named holder no longer owns this open task",
    "error_code": "task_claim_conflict",
}


def _can_release(ctx: RegistryContext, caller: Session, holder: Session, task_id: str) -> bool:
    """A declared claim coordinator on the same project and machine."""
    if caller.project_id != holder.project_id or caller.machine_id != holder.machine_id:
        return False
    return _may_manage_claim(ctx, caller, task_id)


def _may_release_own_claim(
    ctx: RegistryContext, caller: Session, holder: Session, task_id: str
) -> bool:
    """The holder releasing its own claim, when its definition declares release_own_claim."""
    if caller.id != holder.id or caller.machine_id != require_machine_id():
        return False
    definition = session_agent_definition(ctx.task_manager.db, caller)
    task = ctx.task_manager.get_task(task_id)
    return (
        task.project_id == caller.project_id
        and definition is not None
        and definition[0].holds("release_own_claim")
        and task_scope_refusal(
            task, caller.id, definition[0].task_claim_scope, db=ctx.task_manager.db
        )
        is None
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
        from gobby.workflows.checkout_task_attribution import checkout_owned_by_other_claim

        if checkout_owned_by_other_claim(
            ctx.task_manager.db, variables, holder.project_id, task_id, root
        ):
            continue
        found = task_dirty_paths(paths, root)
        if found is None:
            if is_ignorable_attributed_root(
                ctx.task_manager.db,
                project_id=holder.project_id,
                machine_id=holder.machine_id,
                root=root,
            ):
                continue
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
            if holder is None or not (
                _can_release(ctx, caller, holder, resolved_id)
                or _may_release_own_claim(ctx, caller, holder, resolved_id)
            ):
                return {
                    "error": "Caller lacks authority to release this claim",
                    "error_code": "coordinator_required",
                }
            team_refusal = _release_team_refusal(ctx, caller, holder, resolved_id)
            if team_refusal is not None:
                return team_refusal
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
                if not (
                    _can_release(ctx, caller, holder, resolved_id)
                    or _may_release_own_claim(ctx, caller, holder, resolved_id)
                ):
                    return {
                        "error": "Caller lacks authority to release this claim",
                        "error_code": "claim_authority_required",
                    }
                team_refusal = _release_team_refusal(ctx, caller, holder, resolved_id)
                if team_refusal is not None:
                    return team_refusal
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
            "Authorized coordinator or holder: release a named seat's claim on an open "
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


def _may_manage_claim(ctx: RegistryContext, caller: Session, task_id: str) -> bool:
    """The installed definition grants claim coordination for the selected task."""
    if caller.machine_id != require_machine_id():
        return False
    definition = session_agent_definition(ctx.task_manager.db, caller)
    if definition is None or not definition[0].holds("manage_task_claims"):
        return False
    task = ctx.task_manager.get_task(task_id)
    return (
        task.project_id == caller.project_id
        and task_scope_refusal(
            task, caller.id, definition[0].task_claim_scope, db=ctx.task_manager.db
        )
        is None
    )


def _team_refusal(
    ctx: RegistryContext, caller: Session, task_id: str, seats: tuple[Session, ...]
) -> dict[str, Any] | None:
    """Confine a spawned coordinator to its own team's claims and seats.

    An unspawned local root keeps project-wide authority. A spawned coordinator must manage the
    task's team, and each holder or recipient it names must be a seat of its own team or of the
    task's team, so a leaf team's claim moves between that leaf's seats.
    """
    if not (caller.agent_run_id or caller.agent_depth):
        return None
    db = ctx.task_manager.db
    manager = seat_team(db, caller.id)
    team = task_team(db, task_id)
    if (
        manager is None
        or team is None
        or not (team.team_id == manager.team_id or team.parent_team_id == manager.team_id)
    ):
        return {
            "error": "Caller does not manage this task's team",
            "error_code": "team_manager_required",
        }
    allowed = {manager.team_id, team.team_id}
    for seat in seats:
        binding = seat_team(db, seat.id)
        if binding is None or binding.team_id not in allowed:
            return {
                "error": f"Session {seat.id} is not a seat of this task's team",
                "error_code": "team_seat_required",
            }
    return None


def _release_team_refusal(
    ctx: RegistryContext, caller: Session, holder: Session, task_id: str
) -> dict[str, Any] | None:
    """A coordinator's release names one seat; the holder's own release is not team-scoped."""
    if not _can_release(ctx, caller, holder, task_id):
        return None
    return _team_refusal(ctx, caller, task_id, (holder,))


@dataclass(frozen=True)
class ClaimTransfer:
    """A committed claim move: the task as the recipient now holds it, and its event."""

    task: Task
    event_id: int


class ClaimTransferRefused(Exception):
    """A claim move that a check refused; ``payload`` is the error result a caller returns."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("error", "claim transfer refused")))
        self.payload = payload


def transfer_task_claim(
    ctx: RegistryContext,
    *,
    task_id: str,
    holder: Session,
    recipient: Session,
    project_id: str | None,
    actor_id: str,
    reason: str,
) -> ClaimTransfer:
    """Move a claim from ``holder`` to ``recipient`` as one locked transfer.

    The holder check, the dirty-file check and the move commit together. A refusal raises
    ``ClaimTransferRefused`` and rolls the transaction back, so no caller sees a partial move.
    """
    db = ctx.task_manager.db
    # claim_task_for_agent re-takes these exact locks, so the checks and the move commit as one.
    with db.transaction_immediate(AgentTaskClaimMutation(recipient.id)) as conn:
        row = conn.execute(
            "SELECT claimed_by_session_id, project_id, closed_at FROM tasks "
            "WHERE id = %s FOR UPDATE",
            (task_id,),
        ).fetchone()
        if row is None or str(row["project_id"]) != project_id:
            raise ClaimTransferRefused({"error": "Task not found", "error_code": "task_not_found"})
        if row["closed_at"] is not None or str(row["claimed_by_session_id"]) != holder.id:
            raise ClaimTransferRefused(dict(_STALE_HOLDER))
        caller = ctx.session_manager.get(actor_id)
        if (
            caller is None
            or not _can_release(ctx, caller, holder, task_id)
            or recipient.project_id != caller.project_id
            or recipient.machine_id != caller.machine_id
        ):
            raise ClaimTransferRefused(
                {
                    "error": "Caller lacks authority to transfer this claim",
                    "error_code": "claim_authority_required",
                }
            )
        team_refusal = _team_refusal(ctx, caller, task_id, (holder, recipient))
        if team_refusal is not None:
            raise ClaimTransferRefused(team_refusal)
        if holder.id == recipient.id:
            raise ClaimTransferRefused(
                {"error": "The recipient already holds the claim", "error_code": "same_seat"}
            )
        conn.acquire_additional_lock(SessionVariableTransfer((holder.id, recipient.id)))
        refusal = _uncommitted_refusal(ctx, holder, task_id)
        if refusal is not None:
            raise ClaimTransferRefused(refusal)
        handed = ctx.task_manager.claim_task_for_agent(
            task_id, session_id=recipient.id, expected_owner=holder.id, handed=True
        )
        event = TaskLifecycleEventManager(db).record_lifecycle_event(
            task_id,
            "claim:held",
            "claim:handed_off",
            f"Handed claim from {holder.id} to {recipient.id}: {reason}",
            by_actor=actor_id,
        )
    return ClaimTransfer(task=handed, event_id=event.id)


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
            if not _may_manage_claim(ctx, caller, resolved_id):
                return {
                    "error": "Caller lacks authority to hand off this task's claim",
                    "error_code": "claim_authority_required",
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
                or holder.project_id != caller.project_id
                or recipient.project_id != caller.project_id
                or holder.machine_id != caller.machine_id
                or recipient.machine_id != caller.machine_id
            ):
                return {
                    "error": "The holder and recipient must belong to this project and machine",
                    "error_code": "claim_participant_scope",
                }
            team_refusal = _team_refusal(ctx, caller, resolved_id, (holder, recipient))
            if team_refusal is not None:
                return team_refusal
            transfer = transfer_task_claim(
                ctx,
                task_id=resolved_id,
                holder=holder,
                recipient=recipient,
                project_id=caller.project_id,
                actor_id=caller.id,
                reason=reason.strip(),
            )
            record_claimant_state(ctx, recipient.id, transfer.task)
            return {
                "success": True,
                "task_id": resolved_id,
                "handed_from_session_id": holder.id,
                "handed_to_session_id": recipient.id,
                "handed_by_session_id": caller.id,
                "reason": reason.strip(),
                "event_id": transfer.event_id,
            }
        except ClaimTransferRefused as refused:
            return refused.payload
        except (TaskNotFoundError, ValueError) as exc:
            return {"error": str(exc)}

    registry.register(
        name="hand_off_task_claim",
        description=(
            "With manage_task_claims and declared task scope, move the claim to a session on the "
            "same project and local machine. The holder must still "
            "own the task and have no uncommitted task-attributed files; the transfer is atomic "
            "and recorded with the reason."
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
