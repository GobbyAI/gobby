"""Resolve the terminal source and actor access for workspace panes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gobby.storage.project_checkouts import require_root
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.storage.workspaces import Workspace, WorkspaceManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.terminals.actor_scope import (
    OPERATOR_ACTOR,
    ActorScope,
    ActorScopeError,
    resolve_actor_scope,
)
from gobby.terminals.runtime import (
    TerminalRuntime,
    TerminalRuntimeRegistry,
    UnregisteredBackendError,
)
from gobby.terminals.workspace_contract import (
    WorkspaceOpError,
    _pane_of,
    _pane_ref,
    storage_errors,
)


@dataclass(frozen=True, slots=True)
class ShellSpawn:
    runtime: TerminalRuntime
    cwd: str


class WorkspacePaneAccess:
    """Select owned shells or adopted terminals and enforce pane actor scope."""

    def __init__(
        self,
        *,
        workspaces: WorkspaceManager,
        terminals: TerminalManager,
        registry: TerminalRuntimeRegistry,
        sessions: SessionManager,
    ) -> None:
        self._workspaces = workspaces
        self._terminals = terminals
        self._registry = registry
        self._sessions = sessions

    def scope(self, actor: str) -> ActorScope:
        try:
            return resolve_actor_scope(self._sessions, actor)
        except ActorScopeError as exc:
            raise WorkspaceOpError("forbidden", str(exc)) from exc

    @staticmethod
    def require_admitted(scope: ActorScope, terminal: Terminal) -> None:
        if not scope.admits(project_id=terminal.project_id, session_id=terminal.session_id):
            raise WorkspaceOpError(
                "forbidden",
                f"Terminal {terminal.id} is outside the actor's project and agent tree",
            )

    def source(
        self,
        actor: str,
        workspace: Workspace,
        project_id: str,
        worktree_id: str | None,
        terminal_id: str | None,
        cwd: str | None,
    ) -> ShellSpawn | Terminal:
        """Check scope and resolve what fills a new pane, before any row is inserted."""
        scope = self.scope(actor)
        if terminal_id is not None:
            return self._adoptable(scope, workspace, terminal_id)
        if not scope.admits(project_id=project_id):
            raise WorkspaceOpError(
                "forbidden", f"Project {project_id} is outside the actor's project and agent tree"
            )
        try:
            runtime = self._registry.resolve("native")
        except UnregisteredBackendError as exc:
            raise WorkspaceOpError(
                "terminal_failed", "The native terminal runtime is unavailable"
            ) from exc
        if cwd is not None:
            if (
                actor != OPERATOR_ACTOR
                or project_id != PERSONAL_PROJECT_ID
                or worktree_id is not None
                or not Path(cwd).is_absolute()
            ):
                raise WorkspaceOpError(
                    "invalid_op", "cwd requires an absolute operator personal-project path"
                )
            return ShellSpawn(runtime, cwd)
        with storage_errors():
            if worktree_id is None:
                return ShellSpawn(
                    runtime, require_root(self._workspaces.db, project_id, workspace.machine_id)
                )
            worktree = LocalWorktreeManager(self._workspaces.db).get(worktree_id)
        if (
            worktree is None
            or worktree.project_id != project_id
            or worktree.machine_id != workspace.machine_id
        ):
            raise WorkspaceOpError(
                "not_found", f"Worktree {worktree_id} of project {project_id} is not on this node"
            )
        return ShellSpawn(runtime, worktree.worktree_path)

    def _adoptable(self, scope: ActorScope, workspace: Workspace, terminal_id: str) -> Terminal:
        try:
            terminal = self._terminals.get(terminal_id)
        except ValueError as exc:
            raise WorkspaceOpError(
                "invalid_ref", f"Terminal id {terminal_id!r} is invalid"
            ) from exc
        if terminal is None:
            raise WorkspaceOpError("not_found", f"Terminal {terminal_id} not found")
        self.require_admitted(scope, terminal)
        if terminal.state != "live" or terminal.machine_id != workspace.machine_id:
            raise WorkspaceOpError(
                "invalid_op", f"Terminal {terminal_id} is not live on the workspace's node"
            )
        self.refuse_held(terminal.id)
        return terminal

    def refuse_held(self, terminal_id: str) -> None:
        """Raise ``busy`` naming the pane that already holds ``terminal_id``."""
        holder = self._workspaces.get_pane_for_terminal(terminal_id)
        if holder is None:
            return
        with storage_errors():
            target = self._workspaces.resolve_reference(holder.id)
        tab, pane = _pane_of(target, holder.id)
        raise WorkspaceOpError(
            "busy",
            f"Terminal {terminal_id} is held by pane "
            f"{_pane_ref(target.node, target.workspace, tab, pane)}",
        )
