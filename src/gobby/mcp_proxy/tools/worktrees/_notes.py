"""Worktree note tool: owner, next action and blocker for a registered worktree."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.storage.worktree_notes import WorktreeNoteStore
from gobby.utils.datetime import utc_now


def _text_problem(name: str, value: str, *, required: bool) -> str | None:
    if required and not value.strip():
        return f"{name} must not be blank"
    if "\n" in value or "\r" in value:
        return f"{name} must be a single line"
    return None


def create_notes_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with the worktree note tool.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with the set_worktree_note tool
    """
    registry = InternalToolRegistry(
        name="gobby-worktrees-notes",
        description="Owner, next action and blocker notes for registered worktrees",
    )

    @registry.tool(
        name="set_worktree_note",
        description=(
            "Record the owner, next action and blocker for a registered worktree. The status "
            "report shows the note only while it is newer than the tree's last commit or dirty "
            "change. An empty blocker records that nothing blocks the tree."
        ),
    )
    def set_worktree_note(
        worktree_id: str,
        owner: str,
        next_action: str,
        blocker: str,
    ) -> dict[str, Any]:
        """Record a worktree's owner, next action and blocker, replacing any earlier note.

        Args:
            worktree_id: The worktree ID (full UUID or unique id prefix).
            owner: Who owns the tree now. Required, single line.
            next_action: The step that moves the tree forward. Required, single line.
            blocker: What blocks the tree, or empty when nothing does. Single line.

        Returns:
            Dict with success status and the note's timestamp.
        """
        try:
            worktree_id = ctx.resolve_worktree_id(worktree_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        if ctx.worktree_storage.get(worktree_id) is None:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        problem = (
            _text_problem("owner", owner, required=True)
            or _text_problem("next_action", next_action, required=True)
            or _text_problem("blocker", blocker, required=False)
        )
        if problem is not None:
            return {"success": False, "error": problem}

        written_at = utc_now()
        WorktreeNoteStore(ctx.worktree_storage.db).upsert(
            worktree_id,
            owner=owner.strip(),
            next_action=next_action.strip(),
            blocker=blocker.strip(),
            updated_at=written_at,
        )
        return {"success": True, "updated_at": written_at.isoformat()}

    return registry
