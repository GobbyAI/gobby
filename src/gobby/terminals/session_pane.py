"""Resolve the workspace pane a session's terminal occupies."""

from __future__ import annotations

from gobby.storage.session_models import Session
from gobby.storage.terminals import TerminalManager
from gobby.storage.workspace_layout import WorkspaceNotFoundError
from gobby.storage.workspaces import WorkspaceManager, WorkspaceTarget
from gobby.terminals.lookup import ACTIVE_TERMINAL_STATES


def session_pane(workspaces: WorkspaceManager, session: Session) -> WorkspaceTarget | None:
    """The session's pane in its own project and machine, or None when it has none.

    The newest placed active terminal wins over the session's terminal-context hints.
    """
    context = session.terminal_context or {}
    pane = next(
        (
            pane
            for terminal in TerminalManager(workspaces.db).list_for_session(session.id)
            if terminal.state in ACTIVE_TERMINAL_STATES
            if (pane := workspaces.get_pane_for_terminal(terminal.id)) is not None
        ),
        None,
    )
    pane_ref = pane.id if pane else context.get("gobby_pane_ref") or context.get("pane_ref")
    if not pane_ref:
        terminal = context.get("gobby_terminal_id")
        pane = workspaces.get_pane_for_terminal(terminal) if isinstance(terminal, str) else None
        pane_ref = pane.id if pane else None
    if not isinstance(pane_ref, str):
        return None
    try:
        target = workspaces.resolve_reference(pane_ref, node=session.machine_id)
    except (ValueError, WorkspaceNotFoundError):
        return None
    if (
        target.pane is None
        or target.tab is None
        or target.tab.project_id != session.project_id
        or target.node.id != session.machine_id
    ):
        return None
    return target
