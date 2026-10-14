"""Refuse workspace switches a live agent run cannot carry.

A spawned run's gcode code-index overlay grant and SRT write scope are fixed at
launch to the workspace it is bound to. A worktree or clone the run creates or
claims mid-run stays outside both: gcode answers ``code_overlay_mismatch`` and
writes are denied. Only a resume bound to the new workspace moves them, so the
workspace tools refuse up front and name the frozen scope.
"""

from __future__ import annotations

from typing import Any

from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.session_context import get_current_session_id

FROZEN_WORKSPACE_ERROR_CODE = "run_workspace_frozen"


def _bound_workspace(db: HubDatabase, run: AgentRun) -> str:
    if run.worktree_id is not None:
        worktree = LocalWorktreeManager(db).get(run.worktree_id)
        return worktree.worktree_path if worktree is not None else f"worktree {run.worktree_id}"
    if run.clone_id is not None:
        clone = LocalCloneManager(db).get(run.clone_id)
        return clone.clone_path if clone is not None else f"clone {run.clone_id}"
    return "the checkout it launched in"


def frozen_workspace_refusal(
    db: HubDatabase,
    action: str,
    *,
    claimed_worktree_id: str | None = None,
    claimed_clone_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the refusal payload when the calling run cannot use the workspace.

    A create (no claimed id) is refused only for a run already bound to a
    worktree or clone; an unbound coordinator still creates workspaces for other
    seats. A claim for the caller itself is refused unless it names the run's
    own bound workspace. Callers outside a live agent run are never refused.
    """
    session_id = get_current_session_id()
    if not session_id:
        return None
    run = LocalAgentRunManager(db).get_by_session(session_id)
    if run is None:
        return None
    if claimed_worktree_id is not None or claimed_clone_id is not None:
        if (claimed_worktree_id is not None and claimed_worktree_id == run.worktree_id) or (
            claimed_clone_id is not None and claimed_clone_id == run.clone_id
        ):
            return None
    elif run.worktree_id is None and run.clone_id is None:
        return None
    return {
        "success": False,
        "error_code": FROZEN_WORKSPACE_ERROR_CODE,
        "error": (
            f"{action} refused: agent run {run.id} has its gcode code-index overlay and "
            f"SRT write scope frozen at launch to {_bound_workspace(db, run)}. A workspace "
            "it creates or claims mid-run stays outside both (gcode code_overlay_mismatch, "
            "writes denied). Branch inside the bound workspace instead "
            "(git switch -c <branch> <base>), or ask the Orchestrator to resume this "
            "session bound to the other workspace."
        ),
    }
