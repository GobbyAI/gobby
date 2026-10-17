"""Coordinator-only release of a named seat's task claim."""

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import SessionVariableTransfer
from gobby.storage.session_models import Session
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.tasks.lifecycle_access import is_operator_coordinator
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.task_claim_state import task_edited_checkout_paths, task_edited_file_set
from gobby.workflows.task_dirty_state import task_dirty_paths


def _can_release(ctx: RegistryContext, caller: Session, holder: Session) -> bool:
    if caller.project_id != holder.project_id or caller.machine_id != holder.machine_id:
        return False
    role = ctx.session_var_manager.get_variable_subset(caller.id, ("_agent_type",))
    agent_type = role.get("_agent_type")
    if agent_type == "orchestrator":
        return is_operator_coordinator(ctx.task_manager, caller)
    if agent_type != "lane-manager" or caller.machine_id != require_machine_id():
        return False
    definition = AgentDefinitionManager(ctx.task_manager.db).get_by_name(
        "lane-manager", caller.project_id
    )
    if definition is None or not definition.enabled:
        return False
    runs = LocalAgentRunManager(ctx.task_manager.db)
    manager_run = runs.get(caller.agent_run_id) if caller.agent_run_id else None
    holder_run = runs.get(holder.agent_run_id) if holder.agent_run_id else None
    if (
        manager_run is None
        or manager_run.status != "running"
        or manager_run.agent_name != "lane-manager"
        or manager_run.child_session_id != caller.id
        or holder_run is None
        or holder_run.child_session_id != holder.id
    ):
        return False
    lane = (manager_run.resume_metadata_json or {}).get("lane")
    return bool(lane) and lane == (holder_run.resume_metadata_json or {}).get("lane")


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
            if holder is None or not _can_release(ctx, caller, holder):
                return {
                    "error": "Only the Orchestrator or the holder's Lane Manager may release a claim",
                    "error_code": "coordinator_required",
                }
            resolved_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
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
                    return {
                        "error": "The named holder no longer owns this open task",
                        "error_code": "task_claim_conflict",
                    }
                conn.acquire_additional_lock(SessionVariableTransfer((holder.id,)))
                variables = ctx.session_var_manager.get_variables(holder.id)
                by_root: dict[str, set[str]] = {}
                pairs = task_edited_checkout_paths(variables, resolved_id)
                for root, path in pairs:
                    by_root.setdefault(root, set()).add(path)
                legacy = task_edited_file_set(variables, resolved_id) - {path for _, path in pairs}
                if legacy:
                    legacy_root = holder.workspace_path or ctx.get_project_repo_path(
                        holder.project_id, holder.machine_id
                    )
                    if not legacy_root:
                        return {
                            "error": "Holder checkout is unresolved",
                            "error_code": "checkout_unresolved",
                        }
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
            "Orchestrator or owning Lane Manager: release a named seat's claim on an open task. "
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
