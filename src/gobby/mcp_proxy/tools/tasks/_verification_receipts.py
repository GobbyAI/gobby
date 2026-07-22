"""Task-scoped verification receipt inspection and repair tools."""

from __future__ import annotations

from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.tasks import TaskNotFoundError
from gobby.storage.verification_receipts import VerificationReceiptStore
from gobby.utils.session_context import get_current_session_id

from ._context import RegistryContext
from ._resolution import resolve_task_id_for_mcp


def _current_scope(ctx: RegistryContext) -> tuple[str, str] | dict[str, Any]:
    session_ref = get_current_session_id()
    if not session_ref:
        return {"success": False, "error": "current session context is required"}
    try:
        session_id = ctx.resolve_session_id(session_ref)
        project_id = ctx.resolve_project_from_session(session_id)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    return session_id, project_id


def register_verification_receipt_tools(
    registry: InternalToolRegistry,
    ctx: RegistryContext,
) -> None:
    """Register paginated inspection and one-way assignment tools."""
    store = VerificationReceiptStore(ctx.task_manager.db)

    @registry.tool(
        name="list_task_verification_receipts",
        description=(
            "List paginated verification receipts for a task, the current session's "
            "unassigned receipts, or all receipts in the current session."
        ),
    )
    def list_task_verification_receipts(
        task_id: str | None = None,
        scope: Literal["task", "unassigned", "all"] = "task",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        current = _current_scope(ctx)
        if isinstance(current, dict):
            return current
        session_id, project_id = current

        resolved_task_id: str | None = None
        if task_id:
            try:
                resolved_task_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
            except (TaskNotFoundError, ValueError) as exc:
                return {"success": False, "error": str(exc)}
            task = ctx.task_manager.get_task(resolved_task_id)
            if task is None or task.project_id != project_id:
                return {"success": False, "error": "task is outside the current project"}
        try:
            receipts, total = store.list_page(
                project_id=project_id,
                session_id=session_id,
                scope=scope,
                task_id=resolved_task_id,
                limit=limit,
                offset=offset,
            )
        except (TaskNotFoundError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

        next_offset = offset + len(receipts)
        return {
            "success": True,
            "scope": scope,
            "task_id": resolved_task_id,
            "receipts": [receipt.to_dict() for receipt in receipts],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
                "next_offset": next_offset if next_offset < total else None,
            },
        }

    @registry.tool(
        name="assign_verification_receipts",
        description=(
            "Assign current-project/current-session unassigned verification receipts "
            "to a task claimed by the current session. Reassignment is forbidden."
        ),
    )
    def assign_verification_receipts(
        receipt_ids: list[str],
        task_id: str,
    ) -> dict[str, Any]:
        current = _current_scope(ctx)
        if isinstance(current, dict):
            return current
        session_id, project_id = current
        try:
            resolved_task_id = resolve_task_id_for_mcp(ctx.task_manager, task_id)
            task = ctx.task_manager.get_task(resolved_task_id)
            if task is None or task.project_id != project_id:
                return {"success": False, "error": "task is outside the current project"}
            assigned = store.assign_unassigned(
                project_id=project_id,
                session_id=session_id,
                task_id=resolved_task_id,
                receipt_ids=receipt_ids,
                actor=f"session:{session_id}",
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "task_id": resolved_task_id,
            "assigned_count": len(assigned),
            "receipts": [receipt.to_dict() for receipt in assigned],
        }
