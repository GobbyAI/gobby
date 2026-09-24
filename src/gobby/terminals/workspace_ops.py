"""Workspace ops shared by every surface (plan gclient-workspaces 2.1).

The WebSocket, MCP, and CLI surfaces call these ops with an ``actor`` they
derived (``operator`` or ``session:<id>``); this module never inspects a token.
Every op sweeps its workspace's dead panes first, raises ``WorkspaceOpError``
carrying one of six codes, and publishes a ``WorkspaceEvent`` for each
mutation. Ops that kill, spawn into, adopt, or write a terminal check the
actor's scope (``gobby.terminals.actor_scope``); row-only ops are open to any
actor. The only process-local state, the in-flight spawn guard, lives on the
shared ``WorkspaceManager``, so each surface may build its own ``WorkspaceOps``.
Because that guard is per daemon, ops refuse another node's rows with
``invalid_op`` before sweeping them.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from psycopg import Error as PsycopgError
from psycopg.errors import UniqueViolation

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.detection.safe_regex import InvalidPatternError, RegexOutcome, compile_safe_regex
from gobby.storage.machines import Machine
from gobby.storage.project_checkouts import (
    require_root,
)
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.storage.workspace_address import resolve_launch_workspace
from gobby.storage.workspaces import (
    DEFAULT_WORKSPACE_NAME,
    InvalidWorkspaceOpError,
    LayoutChange,
    Workspace,
    WorkspaceManager,
    WorkspaceNotFoundError,
    WorkspacePane,
    WorkspaceTab,
    WorkspaceTarget,
    mint_pane_id,
)
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.terminals.actor_scope import ActorScope, ActorScopeError, resolve_actor_scope
from gobby.terminals.key_bytes import normalize_named_key
from gobby.terminals.runtime import (
    InputPayloadTooLargeError,
    SnapshotResult,
    TerminalRuntime,
    TerminalRuntimeRegistry,
    TerminalWriteError,
    UnregisteredBackendError,
)
from gobby.terminals.termination import kill_terminal
from gobby.terminals.web_spawn import spawn_web_terminal
from gobby.terminals.workspace_contract import (
    PaneOutputWait,
    WorkspaceEvent,
    WorkspaceEventKind,
    WorkspaceOpError,
    WorkspaceSnapshot,
    _identity_env,
    _pane_of,
    _pane_ref,
    _require_local,
    _tab_of,
    _workspace_of,
    storage_errors,
)
from gobby.terminals.workspace_writes import (
    PaneWrite,
    WorkspacePaneWriteError,
    write_workspace_pane,
)
from gobby.terminals.write_coordinator import IdempotencyConflictError, WriteCoordinator

logger = logging.getLogger(__name__)


def _published_seq(result: object) -> int | None:
    if isinstance(result, bool) or not isinstance(result, int | Mapping):
        return None
    if isinstance(result, int):
        return result
    seq = result.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


_T = TypeVar("_T")

PANE_SHELL_COMMAND = ("zsh",)
PANE_ROWS, PANE_COLS = 24, 80
PANE_SPAWN_TIMEOUT_SECONDS = 30.0
WAIT_CAPTURE_LINES = 200
WAIT_CAPTURE_FAILURE_LIMIT = 3
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_ACTIVE_STATES = frozenset({"pending", "live"})


@dataclass(frozen=True, slots=True)
class _ShellSpawn:
    runtime: TerminalRuntime
    cwd: str


class WorkspaceOps:
    """Execute ``workspace.*``, ``tab.*``, and ``pane.*`` ops for every surface."""

    def __init__(
        self,
        *,
        workspaces: WorkspaceManager,
        terminals: TerminalManager,
        registry: TerminalRuntimeRegistry,
        coordinator: WriteCoordinator,
        sessions: SessionManager,
        publish: Callable[[WorkspaceEvent], Awaitable[object]],
    ) -> None:
        self._workspaces = workspaces
        self._terminals = terminals
        self._registry = registry
        self._coordinator = coordinator
        self._sessions = sessions
        self._publish = publish
        self._publish_fence = asyncio.Lock()
        self._detection_registry = DetectionManifestRegistry(workspaces.db)

    # -- workspaces ---------------------------------------------------------

    async def workspace_create(
        self, actor: str, name: str = DEFAULT_WORKSPACE_NAME, *, node: str | None = None
    ) -> Workspace:
        """Return the node's workspace named ``name``, creating it when missing."""
        machine = await self._db(self._workspaces.resolve_node, node)
        _require_local(machine)
        workspace, created = await self._db_guarded(self._workspaces.create, machine.id, name)
        if created:
            await self._emit("workspace.created", workspace.id, workspace=workspace)
        await self._sweep(workspace.id)
        return workspace

    async def workspace_list(self, actor: str, *, node: str | None = None) -> tuple[Workspace, ...]:
        """Every workspace on the node, lowest ref first."""
        del actor
        return await asyncio.to_thread(self._list_workspaces, node)

    def _list_workspaces(self, node: str | None) -> tuple[Workspace, ...]:
        with storage_errors():
            machine = self._workspaces.resolve_node(node)
            _require_local(machine)
            return tuple(self._workspaces.list_for_node(machine.id))

    def _rename_workspace(self, workspace_id: str, name: str) -> Workspace:
        with storage_errors():
            return self._workspaces.rename(workspace_id, name)

    async def workspace_rename(
        self, actor: str, workspace: str, name: str, *, node: str | None = None
    ) -> Workspace:
        target = _workspace_of(await self._enter(workspace, node), workspace)
        renamed = await asyncio.to_thread(self._rename_workspace, target.id, name)
        await self._emit("workspace.renamed", renamed.id, workspace=renamed)
        return renamed

    async def workspace_close(
        self, actor: str, workspace: str, *, node: str | None = None
    ) -> Workspace:
        """Close every tab and the workspace, then kill the owned live terminals."""
        target = _workspace_of(await self._enter(workspace, node), workspace)
        panes = await self._db(self._workspaces.list_panes, target.id)
        doomed = await self._db(self._closing, actor, panes)
        closed = await self._db_guarded(self._workspaces.close, target.id)
        await self._emit("workspace.closed", closed.id, workspace=closed)
        await self._kill(doomed)
        return closed

    async def workspace_set_focus_hints(
        self,
        actor: str,
        workspace: str,
        *,
        project_id: str | None,
        tab: str | None,
        pane: str | None,
        node: str | None = None,
    ) -> tuple[Workspace, WorkspaceTab | None]:
        target = _workspace_of(await self._enter(workspace, node), workspace)
        tab_id = None if tab is None else _tab_of(await self._db(self._resolve, tab, node), tab).id
        pane_id = (
            None
            if pane is None
            else _pane_of(await self._db(self._resolve, pane, node), pane)[1].id
        )
        hinted, focused = await self._db_guarded(
            self._workspaces.set_focus_hints,
            target.id,
            project_id=project_id,
            tab_id=tab_id,
            pane_id=pane_id,
        )
        await self._emit(
            "focus_hints", hinted.id, workspace=hinted, tabs=() if focused is None else (focused,)
        )
        return hinted, focused

    async def workspace_snapshot(
        self,
        actor: str,
        workspace: str | None = None,
        *,
        node: str | None = None,
        project_id: str | None = None,
    ) -> WorkspaceSnapshot:
        """Sweep and read a whole workspace; absent, the node's default, created on first use.

        A registered ``project_id`` with no explicit workspace resolves that
        project's default workspace instead of the projectless scratch. The rows
        are read on the same thread as the sweep. Sweep removals are published
        before the fence opens, and ``lifecycle_seq`` is that last removal so a
        workspace publish waiting on the fence stays above the watermark.
        """
        if workspace is None and project_id is None:
            workspace = (await self.workspace_create(actor, node=node)).id
        elif workspace is None:
            machine = await self._db(self._workspaces.resolve_node, node)
            _require_local(machine)
            resolved, created = await self._db_guarded(
                resolve_launch_workspace,
                self._workspaces,
                machine.id,
                workspace=None,
                project_id=project_id,
            )
            if created:
                await self._emit("workspace.created", resolved.id, workspace=resolved)
            workspace = resolved.id
        async with self._publish_fence:
            snapshot, change = await self._db(self._snapshot_storage, workspace, node)
            seq = await self._publish_removal(snapshot.workspace.id, change, fenced=False)
        if seq is None:
            return snapshot
        return WorkspaceSnapshot(
            node=snapshot.node,
            workspace=snapshot.workspace,
            tabs=snapshot.tabs,
            panes=snapshot.panes,
            lifecycle_seq=seq,
        )

    # -- tabs ---------------------------------------------------------------

    async def tab_create(
        self,
        actor: str,
        workspace: str,
        project_id: str,
        *,
        worktree_id: str | None = None,
        title: str | None = None,
        terminal_id: str | None = None,
        node: str | None = None,
    ) -> LayoutChange:
        """Make a tab whose first pane spawns a shell in the checkout or adopts ``terminal_id``."""
        target = await self._enter(workspace, node)
        home = _workspace_of(target, workspace)
        source = await self._db(
            self._pane_source, actor, home, project_id, worktree_id, terminal_id
        )
        pane_id = mint_pane_id()
        await self._db(self._workspaces.mark_spawn_in_flight, pane_id)
        try:
            change = await self._db_guarded(
                self._workspaces.create_tab,
                home.id,
                pane_id=pane_id,
                project_id=project_id,
                worktree_id=worktree_id,
                title=title,
            )
            pane = await self._fill(target.node, home, change.tabs[0], change.panes[0], source)
        finally:
            await self._db(self._workspaces.clear_spawn_in_flight, pane_id)
        await self._emit("tab.created", home.id, tabs=change.tabs, panes=(pane,))
        return LayoutChange(panes=(pane,), tabs=change.tabs)

    async def tab_rename(
        self, actor: str, tab: str, title: str | None, *, node: str | None = None
    ) -> WorkspaceTab:
        target = _tab_of(await self._enter(tab, node), tab)
        renamed = await self._db_guarded(self._workspaces.rename_tab, target.id, title)
        await self._emit("tab.renamed", renamed.workspace_id, tabs=(renamed,))
        return renamed

    async def tab_move(
        self,
        actor: str,
        tab: str,
        position: int,
        *,
        workspace: str | None = None,
        node: str | None = None,
    ) -> LayoutChange:
        """Move a tab to ``position`` in its own workspace or in ``workspace``."""
        target = await self._enter(tab, node)
        moving = _tab_of(target, tab)
        source = target.workspace.id
        destination = (
            source
            if workspace is None
            else _workspace_of(await self._db(self._resolve, workspace, node), workspace).id
        )
        change = await self._db_guarded(
            self._workspaces.move_tab,
            moving.id,
            workspace_id=destination,
            position=position,
        )
        await self._emit("tab.moved", destination, tabs=change.tabs)
        if destination != source:
            moved = [row for row in change.tabs if row.id == moving.id]
            await self._emit("tab.moved", source, tabs=moved)
        return change

    async def tab_close(self, actor: str, tab: str, *, node: str | None = None) -> LayoutChange:
        """Close a tab with its panes, then kill the owned live terminals."""
        target = await self._enter(tab, node)
        closing = _tab_of(target, tab)
        panes = await self._db(self._workspaces.list_panes, target.workspace.id)
        doomed = await self._db(
            self._closing, actor, [row for row in panes if row.tab_id == closing.id]
        )
        change = await self._db_guarded(self._workspaces.close_tab, closing.id)
        await self._emit(
            "tab.closed",
            target.workspace.id,
            tabs=change.removed_tabs,
            panes=change.removed_panes,
        )
        await self._kill(doomed)
        return change

    # -- pane layout ----------------------------------------------------------

    async def pane_split(
        self,
        actor: str,
        pane: str,
        axis: str,
        *,
        terminal_id: str | None = None,
        node: str | None = None,
    ) -> LayoutChange:
        """Split ``pane`` with a new pane that spawns a shell or adopts ``terminal_id``.

        The pane row is inserted (terminal NULL, guarded as in flight) before the
        spawn; a failed spawn removes it again and raises ``terminal_failed``.
        """
        target = await self._enter(pane, node)
        tab, beside = _pane_of(target, pane)
        source = await self._db(
            self._pane_source,
            actor,
            target.workspace,
            tab.project_id,
            tab.worktree_id,
            terminal_id,
        )
        pane_id = mint_pane_id()
        await self._db(self._workspaces.mark_spawn_in_flight, pane_id)
        try:
            change = await self._db_guarded(
                self._workspaces.add_pane, pane_id, beside=beside.id, axis=axis
            )
            added = await self._fill(
                target.node, target.workspace, change.tabs[0], change.panes[0], source
            )
        finally:
            await self._db(self._workspaces.clear_spawn_in_flight, pane_id)
        await self._emit("pane.added", target.workspace.id, tabs=change.tabs, panes=(added,))
        return LayoutChange(panes=(added,), tabs=change.tabs)

    async def pane_swap(
        self, actor: str, pane: str, other: str, *, node: str | None = None
    ) -> WorkspaceTab:
        first = _pane_of(await self._enter(pane, node), pane)[1]
        second = _pane_of(await self._db(self._resolve, other, node), other)[1]
        tab = await self._db_guarded(self._workspaces.swap_panes, first.id, second.id)
        await self._emit("pane.swapped", tab.workspace_id, tabs=(tab,))
        return tab

    async def pane_move(
        self,
        actor: str,
        pane: str,
        tab: str,
        *,
        beside: str | None = None,
        axis: str = "horizontal",
        node: str | None = None,
    ) -> LayoutChange:
        """Move a pane beside ``beside`` in ``tab``; its source split collapses.

        Each workspace's event carries only its own tabs; both carry the moved pane,
        as ``tab.move`` hands the source workspace the tab that left it.
        """
        target = await self._enter(pane, node)
        moving = _pane_of(target, pane)[1]
        destination = _tab_of(await self._db(self._resolve, tab, node), tab)
        beside_id = (
            None
            if beside is None
            else _pane_of(await self._db(self._resolve, beside, node), beside)[1].id
        )
        change = await self._db_guarded(
            self._workspaces.move_pane,
            moving.id,
            tab_id=destination.id,
            beside=beside_id,
            axis=axis,
        )
        for workspace_id in dict.fromkeys((destination.workspace_id, target.workspace.id)):
            own_tabs = [row for row in change.tabs if row.workspace_id == workspace_id]
            await self._emit("pane.moved", workspace_id, tabs=own_tabs, panes=change.panes)
        if change.removed_tabs:
            await self._emit("tab.removed", target.workspace.id, tabs=change.removed_tabs)
        return change

    async def pane_resize(
        self, actor: str, pane: str, ratio: float, *, node: str | None = None
    ) -> WorkspaceTab:
        target = _pane_of(await self._enter(pane, node), pane)[1]
        tab = await self._db_guarded(self._workspaces.set_ratio, target.id, ratio)
        await self._emit("pane.resized", tab.workspace_id, tabs=(tab,))
        return tab

    async def pane_rename(
        self, actor: str, pane: str, label: str | None, *, node: str | None = None
    ) -> WorkspacePane:
        target = await self._enter(pane, node)
        renamed = await self._db_guarded(
            self._workspaces.rename_pane,
            _pane_of(target, pane)[1].id,
            label,
        )
        await self._emit("pane.renamed", target.workspace.id, panes=(renamed,))
        return renamed

    async def pane_close(self, actor: str, pane: str, *, node: str | None = None) -> LayoutChange:
        """Remove a pane; kill its terminal when it owns a pending or live one.

        An adopted terminal is released. A pane whose terminal already exited is
        pruned by the entry sweep, which is then the whole op.
        """
        target = await self._db(self._resolve, pane, node)
        pane_id = _pane_of(target, pane)[1].id
        swept = await self._sweep(target.workspace.id)
        if any(row.id == pane_id for row in swept.removed_panes):
            return swept
        closing = _pane_of(await self._db(self._resolve, pane, node), pane)[1]
        doomed = await self._db(self._closing, actor, [closing])
        change = await self._db_guarded(self._workspaces.remove_pane, closing.id)
        await self._publish_removal(target.workspace.id, change)
        await self._kill(doomed)
        return change

    # -- pane terminal I/O ----------------------------------------------------

    async def pane_send_text(
        self,
        actor: str,
        pane: str,
        text: str,
        *,
        submit: bool = False,
        idempotency_key: str | None = None,
        node: str | None = None,
    ) -> PaneWrite:
        return await self._write(
            actor,
            pane,
            node,
            kind="text",
            payload=text,
            submit=submit,
            key=idempotency_key,
            verify_submit=submit,
        )

    async def pane_send_keys(
        self,
        actor: str,
        pane: str,
        keys: str,
        *,
        literal: bool = True,
        idempotency_key: str | None = None,
        node: str | None = None,
    ) -> PaneWrite:
        """Write ``keys`` as ``send_keys`` does: a trailing newline submits, names are keys."""
        if literal and keys.endswith("\n"):
            return await self._write(
                actor,
                pane,
                node,
                kind="text",
                payload=keys.rstrip("\n"),
                submit=True,
                key=idempotency_key,
            )
        named = None if literal else normalize_named_key(keys)
        if not literal and named is None:
            raise WorkspaceOpError(
                "invalid_op", f"Unsupported named key for managed terminal: {keys}"
            )
        return await self._write(
            actor,
            pane,
            node,
            kind="key" if named else "text",
            payload=named or keys,
            submit=False,
            key=idempotency_key,
        )

    async def pane_read(
        self, actor: str, pane: str, *, lines: int = 50, node: str | None = None
    ) -> SnapshotResult:
        if lines < 1:
            raise WorkspaceOpError("invalid_op", f"Pane read lines must be positive, not {lines}")
        _row, terminal = await self._pane_terminal(actor, pane, node)
        runtime = self._runtime(terminal)
        try:
            return await runtime.snapshot(terminal, lines)
        except Exception as exc:
            raise WorkspaceOpError("terminal_failed", f"Pane read failed: {exc}") from exc

    async def pane_wait_for_output(
        self,
        actor: str,
        pane: str,
        pattern: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 2.0,
        node: str | None = None,
    ) -> PaneOutputWait:
        """Poll the pane's terminal until ``pattern`` matches, it ends, or time runs out."""
        if not (math.isfinite(timeout_seconds) and math.isfinite(poll_interval_seconds)):
            raise WorkspaceOpError("invalid_op", "Wait durations must be finite numbers")
        # The websocket op has no MCP clamp. 300s matches wait_for_pane_output.
        timeout_seconds = min(timeout_seconds, 300.0)
        try:
            matcher = compile_safe_regex(pattern)
        except InvalidPatternError as exc:
            raise WorkspaceOpError("invalid_op", str(exc)) from exc
        _row, terminal = await self._pane_terminal(actor, pane, node)
        runtime = self._runtime(terminal)
        interval = max(0.1, min(poll_interval_seconds, 30.0))
        deadline = time.monotonic() + timeout_seconds
        failures = 0
        while True:
            snapshot: SnapshotResult | None = None
            try:
                snapshot = await runtime.snapshot(terminal, WAIT_CAPTURE_LINES)
            except Exception:
                logger.warning("Failed to capture terminal %s output", terminal.id, exc_info=True)
            if snapshot is not None:
                failures = 0
                match = matcher.search(snapshot.text)
                if match.outcome is RegexOutcome.PATTERN_TIMEOUT:
                    raise WorkspaceOpError(
                        "invalid_op", "pattern execution exceeded its time budget"
                    )
                if match.matched:
                    return PaneOutputWait(True, "matched", snapshot)
            current = await self._db(self._terminals.get, terminal.id)
            if current is None or current.state not in _ACTIVE_STATES:
                return PaneOutputWait(False, "pane_lost", snapshot)
            if snapshot is None:
                failures += 1
                if failures >= WAIT_CAPTURE_FAILURE_LIMIT:
                    raise WorkspaceOpError(
                        "terminal_failed", "terminal capture failed three consecutive times"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return PaneOutputWait(False, "timeout", snapshot)
            await asyncio.sleep(min(interval, remaining))

    # -- internals ------------------------------------------------------------

    async def _db(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Run one blocking call off the event-loop thread."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _db_guarded(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Run one storage call off the loop and translate storage failures."""

        def run() -> _T:
            with storage_errors():
                return fn(*args, **kwargs)

        return await asyncio.to_thread(run)

    def _resolve(self, reference: str, node: str | None) -> WorkspaceTarget:
        """Resolve a row this node may act on, refusing another node's before any sweep."""
        with storage_errors():
            target = self._workspaces.resolve_reference(reference, node=node)
        _require_local(target.node)
        return target

    async def _enter(self, reference: str, node: str | None) -> WorkspaceTarget:
        """Resolve ``reference`` and sweep its workspace, re-resolving after a prune."""
        target = await asyncio.to_thread(self._resolve, reference, node)
        if (await self._sweep(target.workspace.id)).removed_panes:
            target = await asyncio.to_thread(self._resolve, reference, node)
        return target

    async def _sweep(self, workspace_id: str) -> LayoutChange:
        change = await asyncio.to_thread(self._sweep_storage, workspace_id)
        await self._publish_removal(workspace_id, change)
        return change

    def _sweep_storage(self, workspace_id: str) -> LayoutChange:
        with storage_errors():
            return self._workspaces.sweep_dead_panes(workspace_id)

    def _snapshot_storage(
        self, reference: str, node: str | None
    ) -> tuple[WorkspaceSnapshot, LayoutChange]:
        """Sweep and read rows on one thread so the watermark matches the rows."""
        target = self._resolve(reference, node)
        change = self._sweep_storage(target.workspace.id)
        if change.removed_panes:
            target = self._resolve(reference, node)
        home = _workspace_of(target, reference)
        with storage_errors():
            snapshot = WorkspaceSnapshot(
                node=target.node,
                workspace=home,
                tabs=tuple(self._workspaces.list_tabs(home.id)),
                panes=tuple(self._workspaces.list_panes(home.id)),
            )
        return snapshot, change

    async def _publish_removal(
        self, workspace_id: str, change: LayoutChange, *, fenced: bool = True
    ) -> int | None:
        seq: int | None = None
        if change.removed_panes:
            seq = await self._emit(
                "pane.removed",
                workspace_id,
                tabs=change.tabs,
                panes=change.removed_panes,
                fenced=fenced,
            )
        if change.removed_tabs:
            removed = await self._emit(
                "tab.removed", workspace_id, tabs=change.removed_tabs, fenced=fenced
            )
            if removed is not None:
                seq = removed
        return seq

    async def _emit(
        self,
        kind: WorkspaceEventKind,
        workspace_id: str,
        *,
        workspace: Workspace | None = None,
        tabs: Iterable[WorkspaceTab] = (),
        panes: Iterable[WorkspacePane] = (),
        fenced: bool = True,
    ) -> int | None:
        if not fenced:
            return await self._publish_now(kind, workspace_id, workspace, tabs, panes)
        async with self._publish_fence:
            return await self._publish_now(kind, workspace_id, workspace, tabs, panes)

    async def _publish_now(
        self,
        kind: WorkspaceEventKind,
        workspace_id: str,
        workspace: Workspace | None,
        tabs: Iterable[WorkspaceTab],
        panes: Iterable[WorkspacePane],
    ) -> int | None:
        result = await self._publish(
            WorkspaceEvent(
                kind=kind,
                workspace_id=workspace_id,
                workspace=None if workspace is None else workspace.to_dict(),
                tabs=[tab.to_dict() for tab in tabs],
                panes=[pane.to_dict() for pane in panes],
            )
        )
        return _published_seq(result)

    def _scope(self, actor: str) -> ActorScope:
        try:
            return resolve_actor_scope(self._sessions, actor)
        except ActorScopeError as exc:
            raise WorkspaceOpError("forbidden", str(exc)) from exc

    @staticmethod
    def _require_admitted(scope: ActorScope, terminal: Terminal) -> None:
        if not scope.admits(project_id=terminal.project_id, session_id=terminal.session_id):
            raise WorkspaceOpError(
                "forbidden",
                f"Terminal {terminal.id} is outside the actor's project and agent tree",
            )

    def _pane_source(
        self,
        actor: str,
        workspace: Workspace,
        project_id: str,
        worktree_id: str | None,
        terminal_id: str | None,
    ) -> _ShellSpawn | Terminal:
        """Check scope and resolve what fills a new pane, before any row is inserted."""
        scope = self._scope(actor)
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
        with storage_errors():
            if worktree_id is None:
                return _ShellSpawn(
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
        return _ShellSpawn(runtime, worktree.worktree_path)

    def _adoptable(self, scope: ActorScope, workspace: Workspace, terminal_id: str) -> Terminal:
        try:
            terminal = self._terminals.get(terminal_id)
        except ValueError as exc:
            raise WorkspaceOpError(
                "invalid_ref", f"Terminal id {terminal_id!r} is invalid"
            ) from exc
        if terminal is None:
            raise WorkspaceOpError("not_found", f"Terminal {terminal_id} not found")
        self._require_admitted(scope, terminal)
        if terminal.state != "live" or terminal.machine_id != workspace.machine_id:
            raise WorkspaceOpError(
                "invalid_op", f"Terminal {terminal_id} is not live on the workspace's node"
            )
        self._refuse_held(terminal.id)
        return terminal

    def _refuse_held(self, terminal_id: str) -> None:
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

    async def _fill(
        self,
        node: Machine,
        workspace: Workspace,
        tab: WorkspaceTab,
        pane: WorkspacePane,
        source: _ShellSpawn | Terminal,
    ) -> WorkspacePane:
        """Bind a freshly inserted pane to its adopted or newly spawned terminal."""
        if isinstance(source, Terminal):
            try:
                bound = await self._db(
                    self._workspaces.set_pane_terminal, pane.id, source.id, owns_terminal=False
                )
            except UniqueViolation as exc:
                await self._roll_back(pane.id)
                await self._db(self._refuse_held, source.id)
                raise WorkspaceOpError(
                    "busy", f"Terminal {source.id} is held by another pane"
                ) from exc
        else:
            try:
                result = await spawn_web_terminal(
                    manager=self._terminals,
                    runtime=source.runtime,
                    project_id=tab.project_id,
                    session_id=None,
                    rows=PANE_ROWS,
                    cols=PANE_COLS,
                    cwd=source.cwd,
                    command=list(PANE_SHELL_COMMAND),
                    env=_identity_env(node, workspace, tab, pane),
                    # Bounds the host spawn, so a wedged host cannot hold the pane
                    # in flight (and every close of its workspace busy) forever.
                    timeout_seconds=PANE_SPAWN_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                await self._roll_back(pane.id)
                raise WorkspaceOpError("terminal_failed", f"Pane spawn raised: {exc}") from exc
            if not result.success:
                await self._roll_back(pane.id)
                raise WorkspaceOpError(
                    "terminal_failed", f"Pane spawn failed: {result.error_detail or result.error}"
                )
            bound = await self._db(
                self._workspaces.set_pane_terminal,
                pane.id,
                result.terminal_id,
                owns_terminal=True,
            )
            minted = (
                None
                if bound is not None
                else await self._db(self._terminals.get, result.terminal_id)
            )
            if minted is not None:
                await self._kill([minted])
        if bound is None:
            raise WorkspaceOpError("not_found", f"Pane {pane.id} was removed before it was bound")
        return bound

    async def _roll_back(self, pane_id: str) -> None:
        """Delete an unbound pane and restore the layout of the tab now holding it.

        A concurrent move of the pane also reports not-found, so that is retried
        once; a pane still unbound afterwards is out of flight and the next sweep
        prunes it.
        """
        for _attempt in range(2):
            try:
                change = await self._db(self._workspaces.remove_pane, pane_id)
            except WorkspaceNotFoundError:
                continue
            except (PsycopgError, InvalidWorkspaceOpError) as exc:
                raise WorkspaceOpError(
                    "terminal_failed", f"Unbound pane {pane_id} could not be rolled back: {exc}"
                ) from exc
            home = (change.tabs or change.removed_tabs)[0].workspace_id
            await self._publish_removal(home, change)
            return

    def _closing(self, actor: str, panes: Iterable[WorkspacePane]) -> list[Terminal]:
        """Refuse in-flight panes, then scope-check the owned terminals a close would kill."""
        doomed: list[Terminal] = []
        for pane in panes:
            if pane.terminal_id is None:
                if self._workspaces.is_spawn_in_flight(pane.id):
                    raise WorkspaceOpError(
                        "busy", f"Pane {pane.id} is still spawning; retry after its split replies"
                    )
                continue
            terminal = self._terminals.get(pane.terminal_id) if pane.owns_terminal else None
            if terminal is not None and terminal.state in _ACTIVE_STATES:
                doomed.append(terminal)
        if doomed:
            scope = self._scope(actor)
            for terminal in doomed:
                self._require_admitted(scope, terminal)
        return doomed

    async def _kill(self, terminals: Iterable[Terminal]) -> None:
        """Kill terminals whose pane rows are already gone.

        A failed kill marks a live row orphaned: it stays listed and ``terminal_kill``
        retries it, where a live row behind no pane would be unreachable.
        """
        for terminal in terminals:
            try:
                await kill_terminal(self._terminals, self._registry, terminal)
            except Exception:
                logger.warning(
                    "Failed to kill pane terminal %s; marking it orphaned",
                    terminal.id,
                    exc_info=True,
                )
                await self._db(self._terminals.mark_orphaned, terminal.id)

    async def _pane_terminal(
        self, actor: str, pane: str, node: str | None
    ) -> tuple[WorkspacePane, Terminal]:
        """The pane and the in-scope terminal behind it."""
        scope = await self._db(self._scope, actor)
        row = _pane_of(await self._enter(pane, node), pane)[1]
        terminal = (
            None
            if row.terminal_id is None
            else await self._db(self._terminals.get, row.terminal_id)
        )
        if terminal is None:
            if await self._db(self._workspaces.is_spawn_in_flight, row.id):
                raise WorkspaceOpError("busy", f"Pane {row.id} is still spawning its terminal")
            raise WorkspaceOpError("not_found", f"Pane {row.id} has no terminal")
        self._require_admitted(scope, terminal)
        return row, terminal

    def _runtime(self, terminal: Terminal) -> TerminalRuntime:
        try:
            return self._registry.resolve(terminal.backend)
        except UnregisteredBackendError as exc:
            raise WorkspaceOpError(
                "terminal_failed", f"No runtime serves {terminal.backend} terminals"
            ) from exc

    async def _write(
        self,
        actor: str,
        pane: str,
        node: str | None,
        *,
        kind: Literal["text", "key"],
        payload: str,
        submit: bool,
        key: str | None,
        verify_submit: bool = False,
    ) -> PaneWrite:
        resolved_key = key or secrets.token_hex(16)
        if IDEMPOTENCY_KEY_PATTERN.fullmatch(resolved_key) is None:
            raise WorkspaceOpError(
                "invalid_op", "idempotency_key must be 1 to 128 characters from [A-Za-z0-9._:-]"
            )
        row, terminal = await self._pane_terminal(actor, pane, node)
        try:
            return await write_workspace_pane(
                self._coordinator,
                self._sessions,
                self._detection_registry,
                self._runtime(terminal),
                terminal,
                pane_id=row.id,
                kind=kind,
                payload=payload,
                submit=submit,
                verify_submit=verify_submit,
                idempotency_key=resolved_key,
            )
        except (IdempotencyConflictError, InputPayloadTooLargeError) as exc:
            raise WorkspaceOpError("invalid_op", str(exc)) from exc
        except TerminalWriteError as exc:
            raise WorkspaceOpError("terminal_failed", str(exc)) from exc
        except WorkspacePaneWriteError as exc:
            raise WorkspaceOpError("terminal_failed", str(exc)) from exc
