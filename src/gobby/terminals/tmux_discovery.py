"""Mirror every live tmux pane on this machine into the ``terminals`` table.

Agents create their own rows through ``TerminalRuntime``; a CLI session seeds
its pane at session start. Everything else a user has open — plain shells,
tabs that predate the daemon, panes on the gobby socket whose run already
finished — is only visible through a sweep of the tmux servers themselves.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gobby.agents.tmux.session_manager import TmuxPaneInfo
from gobby.config.tmux import TmuxConfig
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.terminals import (
    ProjectOwnershipConflictError,
    TerminalManager,
    tmux_locator_key,
)
from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)


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


def socket_path_for(config: TmuxConfig) -> str:
    """Canonical socket path tmux will use for ``config`` (matches ``#{socket_path}``)."""
    if config.socket_path:
        return os.path.realpath(config.socket_path)
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return os.path.realpath(
        os.path.join(base, f"tmux-{os.getuid()}", config.socket_name or "default")
    )


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
    Gobby (agent and web spawns) are left to their lifecycle; a pending Gobby
    row whose session name matches a pane is a spawn still being promoted.
    """
    rows = manager.list_live_by_machine(machine_id)
    by_key = {row.locator_key: row for row in rows if row.locator_key}
    pending_names = {
        row.session_name
        for row in rows
        if row.ownership == "gobby" and row.state == "pending" and row.session_name
    }
    seen: dict[str, TmuxPaneInfo] = {}
    swept_sockets: set[str] = set()

    for tmux in tmux_managers:
        try:
            panes = await tmux.list_panes()
        except (TimeoutError, OSError):
            logger.warning("tmux pane listing failed", exc_info=True)
            continue
        if panes is None:
            continue
        swept_sockets.add(socket_path_for(tmux.config))
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
        if row.ownership != "external" or row.backend != "tmux" or not row.locator_key:
            continue
        if row.locator_key in seen:
            continue
        socket = (row.locator or {}).get("socket_path")
        if isinstance(socket, str) and os.path.realpath(socket) in swept_sockets:
            manager.mark_exited(row.id)
    return seen
