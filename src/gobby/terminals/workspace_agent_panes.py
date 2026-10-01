"""Placement lifecycle for agent panes: preflight, reserve, bind, settle and release."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from psycopg.errors import UniqueViolation

from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager, truncate_title
from gobby.storage.workspace_layout import WorkspaceNotFoundError
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

logger = logging.getLogger(__name__)

SEAT_HELD_STATES = frozenset({"pending", "live", "orphaned"})

AgentPlacementErrorCode = Literal[
    "invalid_ref",
    "not_found",
    "forbidden",
    "invalid_op",
    "busy",
    "invalid_placement",
    "seat_live",
    "sandbox_required",
]

# Placement's split directions, mapped to the layout's storage axis.
_SPLIT_AXES = {"right": "horizontal", "down": "vertical"}
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

_SeatKey = tuple[str, str]


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

    @classmethod
    def parse(cls, raw: object) -> AgentPlacement:
        """Validate ``{"tab": {workspace, title}}`` or ``{"split": {pane, axis, title}}``."""
        if not isinstance(raw, Mapping) or len(raw) != 1:
            raise _invalid("placement must be an object with exactly one of 'tab' or 'split'")
        [(kind, body)] = raw.items()
        fields = _PLACEMENT_FIELDS.get(kind) if isinstance(kind, str) else None
        if fields is None:
            raise _invalid(f"placement kind must be 'tab' or 'split', got {kind!r}")
        if not isinstance(body, Mapping) or set(body) != fields:
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
        return cls(kind="split", ref=ref, title=title, axis=stored)


@dataclass(frozen=True)
class ResolvedPlacement:
    """A preflighted placement, with the rows it resolved as read."""

    placement: AgentPlacement
    workspace_id: str
    project_id: str
    seat: str
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
    seat: str
    tab: WorkspaceTab


class AgentPaneReserver:
    """Owns the per-workspace seat locks and in-flight seat entries for placed agents."""

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
        self._seats: dict[_SeatKey, str] = {}
        # Strong refs for shielded cleanups that outlive a cancelled reserve.
        self._cleanups: set[asyncio.Task[None]] = set()

    async def preflight(
        self, actor: str, project_id: str, placement: AgentPlacement
    ) -> ResolvedPlacement:
        """Resolve and authorize ``placement`` read-only; refuse a live seat."""
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
            seat = truncate_title(placement.title) or placement.title
            if self._seat_held(workspace.id, seat):
                raise AgentPlacementError("seat_live", f"Seat {seat!r} is held")
        return ResolvedPlacement(
            placement=placement,
            workspace_id=workspace.id,
            project_id=project_id,
            seat=seat,
            node_ref=target.node.ref,
            workspace_ref=workspace.ref,
            beside_pane_id=beside_pane_id,
            tab_id=tab_id,
        )

    def _seat_held(self, workspace_id: str, seat: str) -> bool:
        """Whether a tab title or pane label equal to ``seat`` holds a held terminal."""
        row = self._workspaces.db.fetchone(
            """
            SELECT 1 FROM workspace_panes p
            JOIN workspace_tabs t ON t.id = p.tab_id
            JOIN terminals term ON term.id = p.terminal_id
            WHERE t.workspace_id = %s
              AND (t.title = %s OR p.label = %s)
              AND term.state = ANY(%s)
            LIMIT 1
            """,
            (workspace_id, seat, seat, sorted(SEAT_HELD_STATES)),
        )
        return row is not None

    async def reserve(
        self, resolved: ResolvedPlacement, *, worktree_id: str | None
    ) -> ReservedPane:
        """Insert the unbound pane under the seat lock; compensate on any failure."""
        key = (resolved.workspace_id, resolved.seat)
        lock = self._locks.setdefault(resolved.workspace_id, asyncio.Lock())
        async with lock:
            if key in self._seats or await asyncio.to_thread(
                self._seat_held, resolved.workspace_id, resolved.seat
            ):
                raise AgentPlacementError("seat_live", f"Seat {resolved.seat!r} is held")
            # No await between the seat check and these marks.
            pane_id = mint_pane_id()
            self._seats[key] = pane_id
            self._workspaces.mark_spawn_in_flight(pane_id)
            insert = asyncio.ensure_future(
                asyncio.to_thread(self._insert, resolved, pane_id, worktree_id)
            )
            with _placement_errors():
                try:
                    tab, pane = await asyncio.shield(insert)
                except BaseException:
                    await self._shielded(self._abandon(insert, key, pane_id))
                    raise
        return ReservedPane(
            pane_id=pane.id,
            tab_id=tab.id,
            workspace_id=resolved.workspace_id,
            pane_ref=f"{resolved.node_ref}:{resolved.workspace_ref}:{tab.ref}:{pane.ref}",
            tab_ref=f"{resolved.node_ref}:{resolved.workspace_ref}:{tab.ref}",
            kind=resolved.placement.kind,
            seat=resolved.seat,
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
            axis=placement.axis,
            expected_workspace_id=resolved.workspace_id,
            expected_tab_id=resolved.tab_id,
            expected_project_id=resolved.project_id,
        )
        return change.tabs[0], self._workspaces.rename_pane(pane_id, placement.title)

    async def _shielded(self, cleanup: Coroutine[Any, Any, None]) -> None:
        """Run ``cleanup`` to completion even if the caller is cancelled again."""
        task = asyncio.ensure_future(cleanup)
        self._cleanups.add(task)
        task.add_done_callback(self._cleanups.discard)
        await asyncio.shield(task)

    async def _abandon(self, insert: asyncio.Future[Any], key: _SeatKey, pane_id: str) -> None:
        """Wait for the insert to settle so no commit lands after the rollback."""
        try:
            await asyncio.wait([insert])
            await self._roll_back(pane_id)
        finally:
            self._clear(key, pane_id)

    async def _roll_back(self, pane_id: str) -> None:
        """Remove an unbound pane; a failure leaves residue the next sweep prunes."""
        try:
            change = await asyncio.to_thread(self._workspaces.remove_pane, pane_id)
        except WorkspaceNotFoundError:
            return
        except Exception as exc:
            _warn("reserve", "rollback", pane_id, None, exc)
            return
        await self._publish_removal("reserve", change, pane_id, None)

    async def bind(self, reserved: ReservedPane, terminal_id: str) -> WorkspacePane:
        """Bind the launch terminal and announce the pane; the mark and seat stay."""
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

        A terminal whose kill failed, or that is orphaned, keeps the pane bound so it
        holds the seat until the terminal settles.
        """
        try:
            if terminal_id is not None and await self._holds_seat(reserved.pane_id, terminal_id):
                await self._keep(reserved.pane_id, terminal_id)
            else:
                await self._remove(reserved.pane_id, terminal_id)
        finally:
            self.settle(reserved)

    async def _holds_seat(self, pane_id: str, terminal_id: str) -> bool:
        """Kill the launch terminal when active; True when it still holds the seat."""
        try:
            terminal = await asyncio.to_thread(self._terminals.get, terminal_id)
        except Exception as exc:
            _warn("release", "read", pane_id, terminal_id, exc)
            return True
        if terminal is None or terminal.state == "exited":
            return False
        if terminal.state not in _KILL_STATES:
            return True
        try:
            await kill_terminal(self._terminals, self._registry, terminal)
        except Exception as exc:
            _warn("release", "kill", pane_id, terminal_id, exc)
            try:
                await asyncio.to_thread(
                    self._terminals.mark_kill_failed,
                    terminal.id,
                    attempt_generation=terminal.attempt_generation,
                    attempt_started_at=terminal.attempt_started_at,
                )
            except Exception as mark_exc:
                _warn("release", "mark_kill_failed", pane_id, terminal_id, mark_exc)
            return True
        return False

    async def _keep(self, pane_id: str, terminal_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._workspaces.set_pane_terminal, pane_id, terminal_id, owns_terminal=True
            )
        except Exception as exc:
            _warn("release", "bind", pane_id, terminal_id, exc)

    async def _remove(self, pane_id: str, terminal_id: str | None) -> None:
        try:
            change = await asyncio.to_thread(self._workspaces.remove_pane, pane_id)
        except WorkspaceNotFoundError:
            return
        except Exception as exc:
            _warn("release", "remove", pane_id, terminal_id, exc)
            return
        await self._publish_removal("release", change, pane_id, terminal_id)

    async def _publish_removal(
        self, operation: str, change: LayoutChange, pane_id: str, terminal_id: str | None
    ) -> None:
        try:
            workspace_id = (change.tabs or change.removed_tabs)[0].workspace_id
            if change.removed_panes:
                await self._emit(
                    "pane.removed", workspace_id, tabs=change.tabs, panes=change.removed_panes
                )
            if change.removed_tabs:
                await self._emit("tab.removed", workspace_id, tabs=change.removed_tabs)
        except Exception as exc:
            _warn(operation, "publish", pane_id, terminal_id, exc)

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
        """Clear the in-flight mark and the seat entry; the bound pane holds the seat."""
        self._clear((reserved.workspace_id, reserved.seat), reserved.pane_id)

    def _clear(self, key: _SeatKey, pane_id: str) -> None:
        self._workspaces.clear_spawn_in_flight(pane_id)
        if self._seats.get(key) == pane_id:
            del self._seats[key]


def _warn(
    operation: str, phase: str, pane_id: str, terminal_id: str | None, exc: BaseException
) -> None:
    """Log a failed step by exception type only: messages may carry secrets."""
    logger.warning(
        "Agent pane %s %s failed: pane=%s terminal=%s error=%s",
        operation,
        phase,
        pane_id,
        terminal_id,
        type(exc).__name__,
    )
