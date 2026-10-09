"""Placement lifecycle for agent panes: preflight, reserve, bind, settle and release."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Literal

from psycopg.errors import UniqueViolation

from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.storage.workspace_layout import MIN_PANE_ROWS
from gobby.storage.workspaces import (
    LayoutChange,
    WorkspaceBusyError,
    WorkspaceManager,
    WorkspacePane,
    WorkspaceTab,
    mint_pane_id,
)
from gobby.terminals.actor_scope import ActorScopeError, resolve_actor_scope
from gobby.terminals.runtime import TerminalRuntimeRegistry
from gobby.terminals.termination import kill_terminal
from gobby.terminals.workspace_contract import (
    WorkspaceEvent,
    WorkspaceEventKind,
    WorkspaceOpError,
    _pane_of,
    _require_local,
    _workspace_of,
    storage_errors,
)

AgentPlacementErrorCode = Literal[
    "invalid_ref",
    "not_found",
    "forbidden",
    "invalid_op",
    "busy",
    "invalid_placement",
    "sandbox_required",
]

# Placement's split directions, mapped to the layout's storage axis.
_SPLIT_AXES = {"right": "horizontal", "down": "vertical", "balanced": "balanced"}
_PLACEMENT_FIELDS = {
    "tab": frozenset({"workspace", "title"}),
    "split": frozenset({"pane", "axis", "title"}),
}
# Terminal states release kills; an orphaned row is left for its reaper.
_KILL_STATES = frozenset({"pending", "live"})
_SHARED_CODES: dict[str, AgentPlacementErrorCode] = {
    "invalid_ref": "invalid_ref",
    "not_found": "not_found",
    "forbidden": "forbidden",
    "invalid_op": "invalid_op",
    "busy": "busy",
}


class AgentPlacementError(Exception):
    """A placement refusal; ``code`` is the reply's machine-readable reason."""

    def __init__(self, code: AgentPlacementErrorCode, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code: AgentPlacementErrorCode = code


@contextmanager
def _placement_errors() -> Iterator[None]:
    """Translate workspace op and storage failures into placement refusals."""
    try:
        with storage_errors():
            try:
                yield
            except WorkspaceBusyError as exc:
                raise WorkspaceOpError("busy", str(exc)) from exc
    except WorkspaceOpError as exc:
        raise AgentPlacementError(_SHARED_CODES.get(exc.code, "invalid_op"), str(exc)) from exc


def _invalid(message: str) -> AgentPlacementError:
    return AgentPlacementError("invalid_placement", message)


@dataclass(frozen=True)
class AgentPlacement:
    """A validated ``placement`` input: a new tab, or a split beside a pane."""

    kind: Literal["tab", "split"]
    ref: str
    title: str
    axis: str | None = None
    columns: int = 80
    rows: int | None = None

    @classmethod
    def parse(cls, raw: object) -> AgentPlacement:
        """Validate ``{"tab": {workspace, title}}`` or ``{"split": {pane, axis, title}}``."""
        if not isinstance(raw, Mapping) or len(raw) != 1:
            raise _invalid("placement must be an object with exactly one of 'tab' or 'split'")
        [(kind, body)] = raw.items()
        fields = _PLACEMENT_FIELDS.get(kind) if isinstance(kind, str) else None
        if fields is None:
            raise _invalid(f"placement kind must be 'tab' or 'split', got {kind!r}")
        allowed = fields | {"columns", "rows"} if kind == "split" else fields
        if not isinstance(body, Mapping) or not fields <= set(body) <= allowed:
            raise _invalid(f"placement.{kind} must have exactly the fields {sorted(fields)}")
        title, ref = body["title"], body["workspace" if kind == "tab" else "pane"]
        if not isinstance(title, str) or not title.strip():
            raise _invalid(f"placement.{kind}.title must be a non-blank string")
        if not isinstance(ref, str) or not ref.strip():
            raise _invalid(f"placement.{kind} must name its target")
        if kind == "tab":
            return cls(kind="tab", ref=ref, title=title)
        axis = body["axis"]
        stored = _SPLIT_AXES.get(axis) if isinstance(axis, str) else None
        if stored is None:
            raise _invalid(f"placement.split.axis must be one of {sorted(_SPLIT_AXES)}")
        columns = body.get("columns", 80)
        if (
            not isinstance(columns, int)
            or isinstance(columns, bool)
            or columns < 80
            or ("columns" in body and stored != "balanced")
        ):
            raise _invalid("placement.split.columns requires balanced axis and at least 80 columns")
        rows = body.get("rows")
        if stored == "balanced":
            if not isinstance(rows, int) or isinstance(rows, bool) or rows < MIN_PANE_ROWS:
                raise _invalid(
                    f"Balanced placement requires viewport rows (at least {MIN_PANE_ROWS})"
                )
        elif "rows" in body:
            raise _invalid("placement.split.rows requires balanced axis")
        return cls(kind="split", ref=ref, title=title, axis=stored, columns=columns, rows=rows)


@dataclass(frozen=True)
class ResolvedPlacement:
    """A preflighted placement, with the rows it resolved as read."""

    placement: AgentPlacement
    workspace_id: str
    project_id: str
    node_ref: int
    workspace_ref: int
    beside_pane_id: str | None = None
    tab_id: str | None = None


@dataclass(frozen=True)
class ReservedPane:
    """An inserted, in-flight agent pane awaiting its terminal."""

    pane_id: str
    tab_id: str
    workspace_id: str
    pane_ref: str
    tab_ref: str
    kind: Literal["tab", "split"]
    tab: WorkspaceTab


class AgentPaneReserver:
    """Owns the per-workspace insert locks for placed agent panes.

    Seats are told apart by ``project#session_ref``; a pane title never refuses a
    placement.
    """

    def __init__(
        self,
        *,
        workspaces: WorkspaceManager,
        terminals: TerminalManager,
        registry: TerminalRuntimeRegistry,
        sessions: SessionManager,
        publish: Callable[[WorkspaceEvent], Awaitable[object]],
    ) -> None:
        self._workspaces = workspaces
        self._terminals = terminals
        self._registry = registry
        self._sessions = sessions
        self._publish = publish
        self._locks: dict[str, asyncio.Lock] = {}

    async def preflight(
        self, actor: str, project_id: str, placement: AgentPlacement
    ) -> ResolvedPlacement:
        """Resolve and authorize ``placement`` read-only."""
        return await asyncio.to_thread(self._preflight, actor, project_id, placement)

    def _preflight(
        self, actor: str, project_id: str, placement: AgentPlacement
    ) -> ResolvedPlacement:
        with _placement_errors():
            target = self._workspaces.resolve_reference(placement.ref)
            try:
                scope = resolve_actor_scope(self._sessions, actor)
            except ActorScopeError as exc:
                raise AgentPlacementError("forbidden", str(exc)) from exc
            if not scope.admits(project_id=project_id):
                raise AgentPlacementError("forbidden", f"Project {project_id} is out of scope")
            _require_local(target.node)
            beside_pane_id = tab_id = None
            if placement.kind == "tab":
                workspace = _workspace_of(target, placement.ref)
            else:
                tab, beside = _pane_of(target, placement.ref)
                if tab.project_id != project_id:
                    raise AgentPlacementError(
                        "forbidden", f"Pane {beside.id} belongs to another project"
                    )
                workspace, beside_pane_id, tab_id = target.workspace, beside.id, tab.id
            if target.node.ref is None:
                raise AgentPlacementError("invalid_op", f"Node {target.node.id} has no ref")
        return ResolvedPlacement(
            placement=placement,
            workspace_id=workspace.id,
            project_id=project_id,
            node_ref=target.node.ref,
            workspace_ref=workspace.ref,
            beside_pane_id=beside_pane_id,
            tab_id=tab_id,
        )

    async def reserve(
        self, resolved: ResolvedPlacement, *, worktree_id: str | None
    ) -> ReservedPane:
        """Insert the unbound pane under the workspace lock; compensate on any failure."""
        lock = self._locks.setdefault(resolved.workspace_id, asyncio.Lock())
        async with lock:
            pane_id = mint_pane_id()
            self._workspaces.mark_spawn_in_flight(pane_id)
            # asyncio.wait leaves the insert running when reserve is cancelled, like
            # shield, without shield's loop-handler report of a later insert failure.
            insert = asyncio.ensure_future(
                asyncio.to_thread(self._insert, resolved, pane_id, worktree_id)
            )
            with _placement_errors():
                try:
                    await asyncio.wait([insert])
                    tab, pane = insert.result()
                except BaseException as exc:
                    interrupted = await self._compensate(insert, pane_id)
                    if interrupted and not isinstance(exc, asyncio.CancelledError):
                        raise asyncio.CancelledError from exc
                    raise
        return ReservedPane(
            pane_id=pane.id,
            tab_id=tab.id,
            workspace_id=resolved.workspace_id,
            pane_ref=f"{resolved.node_ref}:{resolved.workspace_ref}:{tab.ref}:{pane.ref}",
            tab_ref=f"{resolved.node_ref}:{resolved.workspace_ref}:{tab.ref}",
            kind=resolved.placement.kind,
            tab=tab,
        )

    def _insert(
        self, resolved: ResolvedPlacement, pane_id: str, worktree_id: str | None
    ) -> tuple[WorkspaceTab, WorkspacePane]:
        placement = resolved.placement
        if placement.kind == "tab":
            change = self._workspaces.create_tab(
                resolved.workspace_id,
                pane_id=pane_id,
                project_id=resolved.project_id,
                worktree_id=worktree_id,
                title=placement.title,
            )
            return change.tabs[0], change.panes[0]
        if resolved.beside_pane_id is None or placement.axis is None:
            raise _invalid("a split placement needs its preflighted pane and axis")
        change = self._workspaces.add_pane(
            pane_id,
            beside=resolved.beside_pane_id,
            axis="vertical" if placement.axis == "balanced" else placement.axis,
            balance_columns=placement.columns if placement.axis == "balanced" else None,
            balance_rows=placement.rows,
            expected_workspace_id=resolved.workspace_id,
            expected_tab_id=resolved.tab_id,
            expected_project_id=resolved.project_id,
        )
        return change.tabs[0], self._workspaces.rename_pane(pane_id, placement.title)

    async def _compensate(
        self,
        insert: asyncio.Future[tuple[WorkspaceTab, WorkspacePane]],
        pane_id: str,
    ) -> bool:
        """Roll back to completion through any cancellation; True if one arrived."""
        cleanup = asyncio.ensure_future(self._abandon(insert, pane_id))
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.wait([cleanup])
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        return interrupted

    async def _abandon(
        self,
        insert: asyncio.Future[tuple[WorkspaceTab, WorkspacePane]],
        pane_id: str,
    ) -> None:
        """Wait for the insert to settle so no commit lands after the rollback."""
        try:
            await asyncio.wait([insert])
            if not insert.cancelled():
                # Retrieve a late insert failure so the loop never reports it as
                # unhandled; the rollback below is its whole handling.
                insert.exception()
            await self._roll_back(pane_id)
        finally:
            self._workspaces.clear_spawn_in_flight(pane_id)

    async def _roll_back(self, pane_id: str) -> None:
        """Remove an unbound pane; a failure leaves residue the next sweep prunes."""
        try:
            change = await asyncio.to_thread(self._workspaces.remove_pane, pane_id)
        except Exception:
            return
        await self._publish_removal(change)

    async def bind(self, reserved: ReservedPane, terminal_id: str) -> WorkspacePane:
        """Bind the launch terminal and announce the pane; the in-flight mark stays."""
        try:
            pane = await asyncio.to_thread(
                self._workspaces.set_pane_terminal,
                reserved.pane_id,
                terminal_id,
                owns_terminal=True,
            )
        except UniqueViolation as exc:
            raise AgentPlacementError(
                "busy", f"Terminal {terminal_id} is held by another pane"
            ) from exc
        if pane is None:
            raise AgentPlacementError("not_found", f"Pane {reserved.pane_id} was removed")
        kind: WorkspaceEventKind = "tab.created" if reserved.kind == "tab" else "pane.added"
        await self._emit(kind, reserved.workspace_id, tabs=(reserved.tab,), panes=(pane,))
        return pane

    async def release(self, reserved: ReservedPane, *, terminal_id: str | None) -> None:
        """Kill an active launch terminal, then remove the pane; never raises.

        A terminal whose kill failed, or that is orphaned, keeps the pane bound until
        the terminal settles.
        """
        try:
            if terminal_id is not None and await self._still_held(terminal_id):
                await self._keep(reserved.pane_id, terminal_id)
            else:
                await self._remove(reserved.pane_id)
        finally:
            self.settle(reserved)

    async def _still_held(self, terminal_id: str) -> bool:
        """Kill the launch terminal when active; True when it may still be running."""
        try:
            terminal = await asyncio.to_thread(self._terminals.get, terminal_id)
        except Exception:
            # An unreadable terminal may still be running: keep its pane.
            return True
        if terminal is None or terminal.state == "exited":
            return False
        if terminal.state not in _KILL_STATES:
            return True
        try:
            await kill_terminal(self._terminals, self._registry, terminal)
        except Exception:
            # Best effort: the pane is kept whether or not the mark lands.
            with suppress(Exception):
                await asyncio.to_thread(
                    self._terminals.mark_kill_failed,
                    terminal.id,
                    attempt_generation=terminal.attempt_generation,
                    attempt_started_at=terminal.attempt_started_at,
                )
            return True
        return False

    async def _keep(self, pane_id: str, terminal_id: str) -> None:
        """Bind the pane to its terminal; an unbound pane is residue the sweep prunes."""
        with suppress(Exception):
            await asyncio.to_thread(
                self._workspaces.set_pane_terminal, pane_id, terminal_id, owns_terminal=True
            )

    async def _remove(self, pane_id: str) -> None:
        """Remove the pane; a failure leaves residue the next sweep prunes."""
        try:
            change = await asyncio.to_thread(self._workspaces.remove_pane, pane_id)
        except Exception:
            return
        await self._publish_removal(change)

    async def _publish_removal(self, change: LayoutChange) -> None:
        """Publish a committed removal; the row is already gone, so a failed publish is dropped."""
        with suppress(Exception):
            workspace_id = (change.tabs or change.removed_tabs)[0].workspace_id
            if change.removed_panes:
                await self._emit(
                    "pane.removed", workspace_id, tabs=change.tabs, panes=change.removed_panes
                )
            if change.removed_tabs:
                await self._emit("tab.removed", workspace_id, tabs=change.removed_tabs)

    async def _emit(
        self,
        kind: WorkspaceEventKind,
        workspace_id: str,
        *,
        tabs: Iterable[WorkspaceTab] = (),
        panes: Iterable[WorkspacePane] = (),
    ) -> None:
        await self._publish(
            WorkspaceEvent(
                kind=kind,
                workspace_id=workspace_id,
                workspace=None,
                tabs=[tab.to_dict() for tab in tabs],
                panes=[pane.to_dict() for pane in panes],
            )
        )

    def settle(self, reserved: ReservedPane) -> None:
        """Clear the pane's in-flight mark."""
        self._workspaces.clear_spawn_in_flight(reserved.pane_id)
