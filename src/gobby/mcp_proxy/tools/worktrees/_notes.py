"""Worktree note tool: owner, next action and blocker for a registered worktree."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.worktree_notes import WorktreeNoteStore
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.agent_resolver import session_agent_definition

WORKTREE_NOTE_CAPABILITY = "manage_worktree_notes"


def _text_problem(name: str, value: str, *, required: bool) -> str | None:
    if required and not value.strip():
        return f"{name} must not be blank"
    if "\n" in value or "\r" in value:
        return f"{name} must be a single line"
    return None


def _caller_may_write_notes(db: HubDatabase, session_id: str | None) -> bool:
    """The caller's installed definition grants writing worktree notes."""
    if session_id is None:
        return False
    caller = SessionManager(db).get(session_id)
    if caller is None or caller.machine_id != require_machine_id():
        return False
    definition = session_agent_definition(db, caller)
    return definition is not None and definition[0].holds(WORKTREE_NOTE_CAPABILITY)


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
        if not _caller_may_write_notes(ctx.worktree_storage.db, get_current_session_id()):
            return {
                "success": False,
                "error": f"Caller's definition must grant {WORKTREE_NOTE_CAPABILITY}",
            }
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
