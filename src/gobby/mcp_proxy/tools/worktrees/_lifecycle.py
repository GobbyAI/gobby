"""Worktree lifecycle tools: claim, release, delete, status transitions, link task."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.mcp_proxy.tools.worktrees._helpers import resolve_project_context
from gobby.mcp_proxy.tools.worktrees._merge_state import is_worktree_git_merged
from gobby.storage.worktrees import WorktreeStatus
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    delete_worktree_transaction,
)
from gobby.worktrees.events import emit_worktree_event
from gobby.worktrees.executor import run_worktree_delete

logger = logging.getLogger(__name__)


def create_lifecycle_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with worktree lifecycle tools.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with claim/release/delete/status/link tools
    """
    registry = InternalToolRegistry(
        name="gobby-worktrees-lifecycle",
        description="Worktree lifecycle operations",
    )

    @registry.tool(
        name="claim_worktree",
        description="Claim ownership of a worktree for an agent session. Accepts #N, N, UUID, or prefix for session_id.",
    )
    def claim_worktree(
        worktree_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Claim a worktree for an agent session.

        Args:
            worktree_id: The worktree ID to claim (full UUID or unique id prefix).
            session_id: Session reference (accepts #N, N, UUID, or prefix) claiming ownership.

        Returns:
            Dict with success status.
        """
        try:
            worktree_id = ctx.resolve_worktree_id(worktree_id)
            resolved_session_id = ctx.resolve_session_id(session_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        updated = ctx.worktree_storage.claim_if_available(
            worktree_id,
            resolved_session_id,
            allowed_existing_session_ids=(None, resolved_session_id),
        )
        if not updated:
            worktree = ctx.worktree_storage.get(worktree_id)
            if not worktree:
                return {"success": False, "error": f"Worktree '{worktree_id}' not found"}
            if worktree.agent_session_id:
                return {
                    "success": False,
                    "error": f"Worktree already claimed by session '{worktree.agent_session_id}'",
                }
            return {"success": False, "error": "Failed to claim worktree"}

        event = emit_worktree_event(
            "worktree_claimed",
            worktree_id=worktree_id,
            project_id=updated.project_id,
            branch_name=updated.branch_name,
            session_id=resolved_session_id,
        )
        return {"success": True, "event": event}

    @registry.tool(
        name="release_worktree",
        description="Release ownership of a worktree.",
    )
    def release_worktree(worktree_id: str) -> dict[str, Any]:
        """Release a worktree from its current owner.

        Args:
            worktree_id: The worktree ID to release (full UUID or unique id prefix).

        Returns:
            Dict with success status.
        """
        try:
            worktree_id = ctx.resolve_worktree_id(worktree_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        updated = ctx.worktree_storage.release(worktree_id)
        if not updated:
            return {"success": False, "error": "Failed to release worktree"}

        event = emit_worktree_event(
            "worktree_released",
            worktree_id=worktree_id,
            project_id=worktree.project_id,
            branch_name=worktree.branch_name,
            session_id=worktree.agent_session_id,
        )
        return {"success": True, "event": event}

    async def delete_worktree(
        worktree_id: str | None = None,
        worktree_path: str | None = None,
        force: bool | str = False,
        force_delete_branch: bool | str = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Delete a worktree completely (handles all cleanup).

        This is the proper way to remove a worktree. It handles:
        - Removes the worktree directory and all temporary files
        - Cleans up git's worktree tracking (.git/worktrees/)
        - Deletes the associated git branch
        - Removes the Gobby database record

        Do NOT manually run `git worktree remove` - use this tool instead.

        Args:
            worktree_id: The registered worktree ID (full UUID or unique id prefix)
                to delete.
            worktree_path: An existing linked worktree path to adopt and delete.
            force: Force deletion even if there are uncommitted changes.
            force_delete_branch: Force-delete the branch even if it is unmerged.
            project_path: Optional path to project root to resolve git context.

        Returns:
            Dict with success status.
        """
        if (worktree_id is None) == (worktree_path is None):
            return {
                "success": False,
                "error": "Provide exactly one of worktree_id or worktree_path",
            }
        if worktree_id is not None:
            try:
                worktree_id = ctx.resolve_worktree_id(worktree_id)
            except ValueError as e:
                return {"success": False, "error": str(e)}

        force = force in (True, "true", "True", "1") if isinstance(force, str) else force
        force_delete_branch = (
            force_delete_branch in (True, "true", "True", "1")
            if isinstance(force_delete_branch, str)
            else force_delete_branch
        )

        resolved_adoption_manager = None
        if worktree_path is not None:
            resolved_manager, resolved_project_id, context_error = resolve_project_context(
                project_path,
                ctx.git_manager,
                ctx.project_id,
            )
            if context_error or resolved_manager is None or resolved_project_id is None:
                return {
                    "success": False,
                    "error": context_error or "Unable to resolve project context",
                }

            try:
                inspected = await asyncio.to_thread(
                    resolved_manager.inspect_worktree,
                    worktree_path,
                )
                base_branch = await asyncio.to_thread(resolved_manager.get_default_branch)
                worktree, adopted = ctx.worktree_storage.register_adopted(
                    project_id=resolved_project_id,
                    branch_name=inspected.branch,
                    worktree_path=inspected.path,
                    base_branch=base_branch,
                )
            except (OSError, ValueError) as error:
                return {"success": False, "error": str(error)}

            worktree_id = worktree.id
            resolved_adoption_manager = resolved_manager
            if adopted:
                try:
                    emit_worktree_event(
                        "worktree_adopted",
                        worktree_id=worktree.id,
                        project_id=worktree.project_id,
                        branch_name=worktree.branch_name,
                        worktree_path=worktree.worktree_path,
                        base_branch=worktree.base_branch,
                    )
                except Exception as event_error:
                    logger.warning(
                        "Failed to emit worktree_adopted event for %s at %s: %s",
                        worktree.id,
                        worktree.worktree_path,
                        event_error,
                        exc_info=True,
                    )

        assert worktree_id is not None

        def resolve_git_manager(_worktree: Any) -> Any:
            if resolved_adoption_manager is not None:
                return resolved_adoption_manager
            resolved = ctx.git_manager
            if project_path:
                try:
                    manager, _, _ = resolve_project_context(project_path, resolved, None)
                    if manager:
                        resolved = manager
                except (ValueError, OSError) as error:
                    logger.debug(
                        "Failed to resolve project context for project_path=%s: %s",
                        project_path,
                        error,
                    )
            return resolved

        request = WorktreeDeletionRequest(
            worktree_id=worktree_id,
            surface=DeletionSurface.MCP,
            force=force,
            force_delete_branch=force_delete_branch,
        )
        result = await run_worktree_delete(
            ctx.worktree_delete_executor,
            lambda boundary: delete_worktree_transaction(
                boundary,
                request=request,
                worktree_storage=ctx.worktree_storage,
                resolve_git_manager=resolve_git_manager,
                task_manager=ctx.task_manager,
            ),
        )
        if not result.found:
            return {"success": True, "already_deleted": True}
        if not result.success:
            response: dict[str, Any] = {
                "success": False,
                "error": result.error or "Worktree deletion was abandoned",
            }
            if result.uncommitted_changes:
                response["uncommitted_changes"] = True
            return response
        return {
            "success": True,
            "artifact_refs_cleared": result.artifact_refs_cleared,
            "event": result.event,
        }

    registry.register(
        name="delete_worktree",
        description=(
            "Delete a registered worktree by ID, or adopt and delete an existing linked "
            "worktree by path."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "worktree_id": {"type": "string"},
                "worktree_path": {"type": "string"},
                "force": {"type": "boolean", "default": False},
                "force_delete_branch": {"type": "boolean", "default": False},
                "project_path": {"type": "string"},
            },
            "oneOf": [
                {"required": ["worktree_id"], "not": {"required": ["worktree_path"]}},
                {"required": ["worktree_path"], "not": {"required": ["worktree_id"]}},
            ],
        },
        func=delete_worktree,
    )

    @registry.tool(
        name="mark_worktree_merged",
        description="Mark a worktree as merged (ready for cleanup).",
    )
    def mark_worktree_merged(worktree_id: str) -> dict[str, Any]:
        """Mark a worktree as merged.

        Args:
            worktree_id: The worktree ID to mark (full UUID or unique id prefix).

        Returns:
            Dict with success status.
        """
        try:
            worktree_id = ctx.resolve_worktree_id(worktree_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}
        if worktree.branch_name is None:
            return {
                "success": False,
                "error": f"Detached worktree '{worktree_id}' cannot be marked as merged",
            }

        git_merged = is_worktree_git_merged(worktree, ctx.git_manager)
        if git_merged is None:
            return {"success": False, "error": "Git manager not available"}
        if not git_merged:
            return {
                "success": False,
                "error": (
                    f"Cannot mark worktree as merged: branch '{worktree.branch_name}' "
                    f"is not merged into '{worktree.base_branch}'"
                ),
            }

        updated = ctx.worktree_storage.mark_merged(worktree_id)
        if not updated:
            return {"success": False, "error": "Failed to mark worktree as merged"}

        return {"success": True}

    @registry.tool(
        name="abandon_worktree",
        description="Mark a worktree as abandoned.",
    )
    def abandon_worktree(worktree_id: str) -> dict[str, Any]:
        """Mark a worktree as abandoned.

        Args:
            worktree_id: The worktree ID to abandon (full UUID or unique id prefix).

        Returns:
            Dict with success status.
        """
        try:
            worktree_id = ctx.resolve_worktree_id(worktree_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        updated = ctx.worktree_storage.mark_abandoned(worktree_id)
        if not updated:
            return {"success": False, "error": "Failed to abandon worktree"}

        return {"success": True}

    @registry.tool(
        name="reactivate_worktree",
        description="Reactivate a worktree without merging or deleting it.",
    )
    def reactivate_worktree(worktree_id: str) -> dict[str, Any]:
        """Reactivate a worktree.

        Args:
            worktree_id: The worktree ID to reactivate (full UUID or unique id prefix).

        Returns:
            Dict with success status.
        """
        try:
            worktree_id = ctx.resolve_worktree_id(worktree_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        updated = ctx.worktree_storage.update(
            worktree_id,
            status=WorktreeStatus.ACTIVE.value,
            merged_at=None,
            cleanup_after=None,
        )
        if not updated:
            return {"success": False, "error": "Failed to reactivate worktree"}

        return {"success": True}

    @registry.tool(
        name="link_task_to_worktree",
        description="Link a task to an existing worktree.",
    )
    def link_task_to_worktree(
        worktree_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Link a task to a worktree.

        Args:
            worktree_id: The worktree ID (full UUID or unique id prefix).
            task_id: The task ID to link.

        Returns:
            Dict with success status.
        """
        try:
            worktree_id = ctx.resolve_worktree_id(worktree_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}
        if worktree.branch_name is None:
            return {
                "success": False,
                "error": f"Detached worktree '{worktree_id}' cannot be linked to a task",
            }

        try:
            resolved_task_id = ctx.resolve_task_id(task_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        updated = ctx.worktree_storage.update(worktree_id, task_id=resolved_task_id)
        if not updated:
            return {"success": False, "error": "Failed to link task to worktree"}

        return {"success": True}

    return registry
