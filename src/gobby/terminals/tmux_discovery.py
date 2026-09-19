"""Mirror every live tmux pane on this machine into the ``terminals`` table.

Agents create their own rows through ``TerminalRuntime``; a CLI session seeds
its pane at session start. Everything else a user has open — plain shells,
tabs that predate the daemon, panes on the gobby socket whose run already
finished — is only visible through a sweep of the tmux servers themselves.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gobby.agents.tmux.session_manager import TmuxPaneInfo
from gobby.config.tmux import TmuxConfig, socket_root
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.terminals import (
    ProjectOwnershipConflictError,
    Terminal,
    TerminalManager,
    tmux_locator_key,
)
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)

# How long a Gobby-spawned pane stays reachable for post-mortem capture after the
# CLI inside it exits. Agent panes carry ``remain-on-exit``, so the corpse is what
# ``capture_full_pane`` reads; without a reaper it is also what outlives the run
# forever.
DEAD_PANE_RETENTION_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class PaneOwner:
    """The Gobby session recorded as running inside a pane."""

    session_id: str
    project_id: str


class PaneLister(Protocol):
    """The slice of ``TmuxSessionManager`` the sweep needs."""

    @property
    def config(self) -> TmuxConfig: ...

    async def list_panes(self) -> list[TmuxPaneInfo] | None: ...

    async def kill_session(self, name: str, *, missing_ok: bool = False) -> bool: ...


def socket_path_for(config: TmuxConfig) -> str:
    """Canonical socket path tmux will use for ``config`` (matches ``#{socket_path}``)."""
    if config.socket_path:
        return os.path.realpath(config.socket_path)
    return os.path.realpath(os.path.join(socket_root(), config.socket_name or "default"))


def pane_owners(sessions: Iterable[Session]) -> dict[tuple[str, str], PaneOwner]:
    """Map ``(socket_path, pane_id)`` to the session whose terminal_context names it."""
    owners: dict[tuple[str, str], PaneOwner] = {}
    for session in sessions:
        context = session.terminal_context or {}
        socket = context.get("tmux_socket_path")
        pane = context.get("tmux_pane")
        if isinstance(socket, str) and socket and isinstance(pane, str) and pane:
            owners[(os.path.realpath(socket), pane)] = PaneOwner(
                session_id=session.id, project_id=session.project_id
            )
    return owners


def _project_for_path(manager: TerminalManager, pane_path: str | None) -> str | None:
    """Registered project owning ``pane_path``; a worktree resolves to its parent."""
    if not pane_path:
        return None
    context = get_project_context(Path(pane_path))
    if context is None:
        return None
    candidate = context.get("parent_project_id") or context.get("id")
    if not isinstance(candidate, str) or not candidate:
        return None
    try:
        registered = LocalProjectManager(manager.db).get(candidate)
    except ValueError:
        return None
    return None if registered is None else registered.id


async def sweep_tmux_terminals(
    manager: TerminalManager,
    tmux_managers: Sequence[PaneLister],
    *,
    machine_id: str,
    owners: Mapping[tuple[str, str], PaneOwner],
    fallback_project_id: str,
) -> dict[str, TmuxPaneInfo]:
    """Upsert an external row per live pane and expire rows whose pane is gone.

    Returns the live panes keyed by ``locator_key`` so callers can decorate
    inventory rows with pane metadata that is not persisted. Rows owned by
    Gobby (agent and web spawns) are never re-labelled from a pane; a pending
    Gobby row whose spawn key (the tmux session name until ``promote_to_live``
    records it as ``session_name``) matches a pane is a spawn still being
    promoted. A live row of either ownership whose pane is missing from a
    socket the sweep could read is expired: a Gobby row outliving its tmux
    server would otherwise stay attachable forever.

    Dead Gobby panes past the retention window are reaped once the mirror is
    settled; see ``reap_expired_dead_panes``.

    The database and project-file work runs off the event loop. The sweep
    fronts every ``terminal_list``, and the loop it would otherwise hold is
    the one carrying keystrokes for every other terminal connection.
    """
    rows = await asyncio.to_thread(manager.list_live_by_machine, machine_id)
    collected: list[tuple[PaneLister, list[TmuxPaneInfo]]] = []
    for tmux in tmux_managers:
        try:
            panes = await tmux.list_panes()
        except (TimeoutError, OSError):
            logger.warning("tmux pane listing failed", exc_info=True)
            continue
        if panes is None:
            continue
        collected.append((tmux, panes))
    listings = [(socket_path_for(tmux.config), panes) for tmux, panes in collected]
    seen = await asyncio.to_thread(
        _reconcile_panes,
        manager,
        rows,
        listings,
        machine_id=machine_id,
        owners=owners,
        fallback_project_id=fallback_project_id,
    )
    await reap_expired_dead_panes(collected)
    return seen


async def reap_expired_dead_panes(
    collected: Sequence[tuple[PaneLister, Sequence[TmuxPaneInfo]]],
    *,
    retention_seconds: float = DEAD_PANE_RETENTION_SECONDS,
    now: float | None = None,
) -> int:
    """Kill Gobby-spawned tmux sessions whose panes have all been dead too long.

    Agent sessions are created with ``remain-on-exit`` so ``capture-pane`` still
    works after the CLI exits, and nothing else clears the corpse:
    ``_reconcile_panes`` skips a dead pane, marks its row exited, and the pane,
    its session and its ``pipe-pane`` child then outlive every cleanup path --
    ``cleanup_terminal_tmux_sessions`` only reaches rows still pending, live or
    orphaned. Waiting out ``retention_seconds`` keeps a run's post-mortem
    capture available for a day before the session goes.

    The user's own tmux server is never touched: only a socket Gobby named for
    itself is reaped, and only sessions carrying that socket's
    ``session_prefix``. A session with any live pane, or any dead pane tmux did
    not date, is left alone -- and killing the session would take a live sibling
    pane with it.
    """
    deadline = (time.time() if now is None else now) - retention_seconds
    reaped = 0
    for tmux, panes in collected:
        if not (tmux.config.socket_name or tmux.config.socket_path):
            # The default socket is the user's personal tmux server; Gobby
            # spawns nothing there, so nothing there is Gobby's to kill.
            continue
        prefix = f"{tmux.config.session_prefix}-"
        by_session: dict[str, list[TmuxPaneInfo]] = defaultdict(list)
        for pane in panes:
            if pane.session_name.startswith(prefix):
                by_session[pane.session_name].append(pane)
        for name, session_panes in by_session.items():
            deaths = [
                pane.pane_dead_time
                for pane in session_panes
                if pane.pane_dead and pane.pane_dead_time is not None
            ]
            if len(deaths) != len(session_panes) or max(deaths) > deadline:
                continue
            try:
                killed = await tmux.kill_session(name, missing_ok=True)
            except (TimeoutError, OSError):
                logger.warning("reaping dead tmux session %s failed", name, exc_info=True)
                continue
            if killed:
                reaped += 1
                logger.info(
                    "Reaped tmux session %s, dead since %s",
                    name,
                    max(deaths),
                )
    return reaped


def _reconcile_panes(
    manager: TerminalManager,
    rows: Sequence[Terminal],
    listings: Sequence[tuple[str, Sequence[TmuxPaneInfo]]],
    *,
    machine_id: str,
    owners: Mapping[tuple[str, str], PaneOwner],
    fallback_project_id: str,
) -> dict[str, TmuxPaneInfo]:
    """Mirror each listed socket's panes into ``rows``; blocking, so run off the loop."""
    by_key = {row.locator_key: row for row in rows if row.locator_key}
    pending_names = {
        name
        for row in rows
        if row.ownership == "gobby" and row.state == "pending"
        for name in (row.session_name, row.spawn_key)
        if name
    }
    seen: dict[str, TmuxPaneInfo] = {}
    swept_sockets: set[str] = set()

    for socket_path, panes in listings:
        swept_sockets.add(socket_path)
        for pane in panes:
            if pane.pane_dead:
                continue
            swept_sockets.add(os.path.realpath(pane.socket_path))
            key = tmux_locator_key(
                socket_path=pane.socket_path,
                server_pid=pane.server_pid,
                server_start_time=pane.server_start_time,
                pane_id=pane.pane_id,
            )
            seen[key] = pane
            existing = by_key.get(key)
            if existing is not None and existing.ownership == "gobby":
                continue
            if pane.session_name in pending_names:
                continue
            owner = owners.get((os.path.realpath(pane.socket_path), pane.pane_id))
            if existing is not None:
                project_id = existing.project_id
            elif owner is not None:
                project_id = owner.project_id
            else:
                project_id = _project_for_path(manager, pane.pane_path) or fallback_project_id
            try:
                manager.upsert_external(
                    machine_id=machine_id,
                    project_id=project_id,
                    backend="tmux",
                    locator={
                        "socket_path": pane.socket_path,
                        "server_pid": pane.server_pid,
                        "server_start_time": pane.server_start_time,
                        "pane_id": pane.pane_id,
                    },
                    locator_key=key,
                    session_name=pane.session_name,
                    window_id=pane.window_id,
                    title=pane.window_name or pane.pane_title or pane.session_name,
                    session_id=None if owner is None else owner.session_id,
                )
            except ProjectOwnershipConflictError:
                logger.debug("pane %s changed project mid-sweep", key)

    for row in rows:
        if row.backend != "tmux" or not row.locator_key or row.locator_key in seen:
            continue
        socket = (row.locator or {}).get("socket_path")
        if isinstance(socket, str) and os.path.realpath(socket) in swept_sockets:
            manager.mark_exited(row.id)
    return seen
