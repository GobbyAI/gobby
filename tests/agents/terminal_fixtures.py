"""Shared factory for agent-suite terminal rows (plan 2.1)."""

from __future__ import annotations

import uuid

from gobby.storage.agents._models import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import (
    Terminal,
    TerminalManager,
    native_locator_key,
    tmux_locator_key,
)
from gobby.utils.machine_id import require_machine_id

_SOCKET = "/private/tmp/tmux-501/default"


def make_pending_terminal(
    run: AgentRun,
    backend: str = "tmux",
    *,
    db: HubDatabase | None = None,
    spawn_key: str | None = None,
) -> Terminal:
    """Create a gobby-owned pending terminal and link it to ``run``."""
    if db is None:
        raise TypeError("make_pending_terminal requires db=")
    manager = TerminalManager(db)
    terminal_id = str(uuid.uuid4())
    key = spawn_key
    if key is None:
        key = terminal_id if backend == "native" else f"gobby-{terminal_id}"
    pending = manager.create_pending(
        terminal_id=terminal_id,
        project_id=_project_id_for_run(db, run),
        backend=backend,
        ownership="gobby",
        spawn_key=key,
        machine_id=run.machine_id or require_machine_id(),
        session_id=run.child_session_id or run.parent_session_id,
        agent_run_id=run.id,
    )
    db.execute(
        "UPDATE agent_runs SET terminal_id = %s WHERE id = %s",
        (pending.id, run.id),
    )
    run.terminal_id = pending.id
    return pending


def make_live_terminal(
    run: AgentRun,
    backend: str = "tmux",
    *,
    db: HubDatabase | None = None,
    session_name: str | None = None,
    window_id: str = "@1",
    title: str | None = None,
    pane_id: str = "%1",
    host_epoch: str | None = None,
    spawn_key: str | None = None,
) -> Terminal:
    """Create a live terminal row for ``run`` and persist the link."""
    if db is None:
        raise TypeError("make_live_terminal requires db=")
    pending = make_pending_terminal(run, backend, db=db, spawn_key=spawn_key)
    manager = TerminalManager(db)
    if backend == "native":
        epoch = host_epoch or str(uuid.uuid4())
        host_terminal_id = str(uuid.uuid4())
        live = manager.promote_to_live(
            pending.id,
            locator={"host_terminal_id": host_terminal_id},
            locator_key=native_locator_key(epoch, host_terminal_id),
            host_epoch=epoch,
            title=title,
        )
    else:
        locator = {
            "socket_path": _SOCKET,
            "server_pid": 1658,
            "server_start_time": 1784592177,
            "pane_id": pane_id,
        }
        live = manager.promote_to_live(
            pending.id,
            locator=locator,
            locator_key=tmux_locator_key(
                socket_path=_SOCKET,
                server_pid=1658,
                server_start_time=1784592177,
                pane_id=pane_id,
            ),
            session_name=session_name or pending.spawn_key,
            window_id=window_id,
            title=title,
        )
    assert live is not None
    return live


def _project_id_for_run(db: HubDatabase, run: AgentRun) -> str:
    row = db.fetchone(
        "SELECT project_id FROM sessions WHERE id = %s",
        (run.parent_session_id,),
    )
    if row is None or row["project_id"] is None:
        raise RuntimeError(f"agent run {run.id} has no parent session project")
    return str(row["project_id"])
