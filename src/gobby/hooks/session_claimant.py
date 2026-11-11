"""Kernel-attested claimant of a machine-bearer hook (#25905).

The machine-wide bearer names no session, so a hook that would create a session's row or
mint its credential must come from the CLI process that owns that session. The daemon
reads the request's local TCP socket from the kernel socket table: its owner is the hook
process, and the stabilized parent of that process is the CLI, named by pid and start
time. The body's own parent pid is never proof.

Ownership is durable. A live terminal row stores its CLI. A deferred startup's mark pins
its CLI, and only that CLI's hook creates the row (session_startup_marks). A thread run by
Codex's shared app-server is registered to that worker (codex_thread_hosts). The first
owner stands: another may replace it only after proving it dead, by pid and start time,
and the replacement is a compare-and-swap, so of two racers one wins.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psutil

from gobby.hooks.terminal_context import _is_codex_shared_host, _stable_cli_process
from gobby.sessions.terminal_kill import create_time_matches
from gobby.storage.codex_thread_hosts import ThreadHost, thread_host
from gobby.storage.session_startup_marks import StartupPin, startup_mark
from gobby.storage.sessions import LIVE_SESSION_STATUSES
from gobby.terminal_context import parse_terminal_context_value

logger = logging.getLogger(__name__)

SESSION_CLAIMANT_MISMATCH = "session_claimant_mismatch"
SESSION_STARTUP_UNPINNED = "session_startup_unpinned"
CLAIMANT_UNVERIFIED = "claimant_unverified"

_PROCESS_ERRORS = (psutil.Error, OSError)


@dataclass(frozen=True)
class Claimant:
    """The process that opened a request's TCP socket, with the CLI it proves.

    ``cli_pid`` and ``create_time`` name the proven CLI. Under Codex's shared app-server
    (``shared_host``) no CLI names one thread: ``parent_pid`` and ``create_time`` then name
    that worker, which runs as this daemon's user. ``cli_pid`` is None when the parent is
    missing or cannot be inspected.
    """

    pid: int
    parent_pid: int | None
    cli_pid: int | None
    shared_host: bool = False
    create_time: float | None = None


@dataclass(frozen=True)
class SessionStartClaim:
    """What a machine-bearer hook may do once its handler has run.

    ``proven`` hooks run with their CLI stamped as the row's owner. A start mints, and so
    does the hook that creates a deferred startup's row (``creates_row``). ``displaced``
    is the dead pin a start replaces; ``host`` and ``displaced_host`` are the shared
    worker a Codex thread registers to and the dead one it replaces.
    """

    refusal: str | None = None
    proven: bool = False
    cli_pid: int | None = None
    create_time: float | None = None
    creates_row: bool = False
    displaced: StartupPin | None = None
    host: ThreadHost | None = None
    displaced_host: ThreadHost | None = None

    @property
    def pin(self) -> StartupPin | None:
        """The proven CLI as a startup pin."""
        if self.cli_pid is None or self.create_time is None:
            return None
        return StartupPin(self.cli_pid, self.create_time)


@dataclass(frozen=True)
class _LiveTerminal:
    session_id: str
    parent_pid: int | None
    raw_create_time: Any


def _socket_owner(*, client_port: int, server_port: int) -> int | None:
    """Return the pid whose TCP socket runs from client_port to the daemon's server_port.

    psutil's system-wide listing aborts on macOS as soon as one process is not
    inspectable, so each process is asked for its own sockets and the ones it cannot
    read are skipped.
    """
    for proc in psutil.process_iter():
        try:
            connections = proc.net_connections(kind="tcp")
        except _PROCESS_ERRORS:
            continue
        for conn in connections:
            if (
                conn.laddr
                and conn.raddr
                and conn.laddr.port == client_port
                and conn.raddr.port == server_port
            ):
                return int(proc.pid)
    return None


PEER_PORT_HEADER = "X-Gobby-Peer-Port"


def front_door_claimant(headers: Mapping[str, str], *, daemon_port: int) -> Claimant | None:
    """Resolve the CLI behind a front-door request from the peer port gdaemon observed.

    Behind gdaemon the backend's own client port is always 0, so it never sees the hook's
    socket. gdaemon injects the accepted socket's port under PEER_PORT_HEADER and strips any
    inbound copy. The caller reads it only after the front-door secret has verified. A
    missing, zero or malformed port proves nothing, so it resolves to no claimant.
    """
    raw = headers.get(PEER_PORT_HEADER, "")
    if not raw.isdecimal():
        return None
    client_port = int(raw)
    if not 0 < client_port <= 65535:
        return None
    return resolve_claimant(client_port=client_port, server_port=daemon_port)


def resolve_claimant(*, client_port: int, server_port: int) -> Claimant | None:
    """Return the process that owns the loopback socket from client_port to the daemon."""
    pid = _socket_owner(client_port=client_port, server_port=server_port)
    if pid is None:
        return None
    unverified = Claimant(pid=pid, parent_pid=None, cli_pid=None)
    try:
        parent = psutil.Process(pid).parent()
        if parent is None:
            return unverified
        if _is_codex_shared_host(parent):
            # The worker is the operator's own; another user's worker proves nothing.
            if parent.uids().real != os.getuid():
                return unverified
            return Claimant(
                pid=pid,
                parent_pid=parent.pid,
                cli_pid=None,
                shared_host=True,
                create_time=parent.create_time(),
            )
        cli = _stable_cli_process(parent)
        started = cli.create_time()
    except _PROCESS_ERRORS:
        return unverified
    return Claimant(pid=pid, parent_pid=parent.pid, cli_pid=cli.pid, create_time=started)


def process_is_dead(pid: int, create_time: float | None) -> bool:
    """Whether the process recorded as ``pid`` started at ``create_time`` is provably gone.

    A missing pid is gone, and so is a pid whose process started at another time: the pid
    was reused. A process that cannot be inspected, or a record with no start time, is
    treated as alive, so nothing is ever taken from a CLI that may still run.
    """
    try:
        started = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return True
    except _PROCESS_ERRORS:
        return False
    return create_time is not None and not create_time_matches(started, create_time)


def _same_process(pid: int, create_time: float | None, other: int, other_time: Any) -> bool:
    if pid != other:
        return False
    if create_time is None or isinstance(other_time, bool):
        return False
    if not isinstance(other_time, (int, float)):
        # A row stored before start times were recorded names its CLI by pid alone.
        return other_time is None
    return create_time_matches(create_time, float(other_time))


def _live_terminal(
    db: Any, *, external_id: str, source: str, machine_id: str
) -> _LiveTerminal | None:
    row = db.fetchone(
        """
        SELECT id, terminal_context FROM sessions
        WHERE external_id = %s AND source = %s AND machine_id = %s
          AND session_type = 'terminal' AND status = ANY(%s)
        ORDER BY created_at DESC, id
        LIMIT 1
        """,
        (external_id, source, machine_id, list(LIVE_SESSION_STATUSES)),
    )
    if row is None:
        return None
    context = parse_terminal_context_value(row["terminal_context"]) or {}
    parent_pid = context.get("parent_pid")
    if isinstance(parent_pid, bool) or not isinstance(parent_pid, int):
        parent_pid = None
    return _LiveTerminal(
        session_id=str(row["id"]),
        parent_pid=parent_pid,
        raw_create_time=context.get("parent_create_time"),
    )


def _take_over_row(db: Any, live: _LiveTerminal, *, cli_pid: int, create_time: float) -> bool:
    """Swap a dead owner's row to the proven CLI; return whether this caller won the swap."""
    cursor = db.execute(
        """
        UPDATE sessions
        SET terminal_context = (COALESCE(terminal_context, '{}'::jsonb) - 'parent_name')
            || jsonb_build_object('parent_pid', %s::int, 'parent_create_time', %s::float8)
        WHERE id = %s
          AND terminal_context -> 'parent_pid' = to_jsonb(%s::int)
          AND (terminal_context -> 'parent_create_time') IS NOT DISTINCT FROM %s::jsonb
        """,
        (
            cli_pid,
            create_time,
            live.session_id,
            live.parent_pid,
            None if live.raw_create_time is None else json.dumps(live.raw_create_time),
        ),
    )
    return bool(cursor.rowcount)


def _refuse(reason: str, message: str, source: str, external_id: str) -> SessionStartClaim:
    logger.warning("Machine hook refused for %s session %s: %s", source, external_id, message)
    return SessionStartClaim(refusal=reason)


def claim_session_start(
    db: Any,
    *,
    claimant: Claimant | None,
    daemon_pid: int,
    external_id: str,
    source: str,
    machine_id: str,
    start: bool,
) -> SessionStartClaim:
    """Decide what a machine-bearer hook may do for its kernel-attested claimant.

    A daemon-internal replay proves nothing and mints nothing. An unresolved claimant is
    refused. A live row's owner proceeds; a start from a new CLI takes over a row whose
    owner is proven dead; anyone else is refused. With no row, a start from any CLI may
    pin the thread unless a live CLI already holds the pin, and only the pinned CLI's
    hook may create the row. A hook with no pin creates nothing.
    """
    if claimant is None:
        return SessionStartClaim(refusal=CLAIMANT_UNVERIFIED)
    if claimant.pid == daemon_pid:
        return SessionStartClaim()
    if claimant.shared_host:
        return _claim_shared_host(
            db,
            claimant,
            external_id=external_id,
            source=source,
            machine_id=machine_id,
            start=start,
        )
    live = _live_terminal(db, external_id=external_id, source=source, machine_id=machine_id)
    cli_pid, started = claimant.cli_pid, claimant.create_time
    if cli_pid is None or started is None:
        # No CLI attests this hook: never mint, and never touch a live row.
        if live is not None:
            return SessionStartClaim(refusal=CLAIMANT_UNVERIFIED)
        logger.warning(
            "Hook for %s session %s mints nothing: its parent is missing or not inspectable",
            source,
            external_id,
        )
        return SessionStartClaim()
    proven = SessionStartClaim(proven=True, cli_pid=cli_pid, create_time=started)
    if live is not None:
        if live.parent_pid is None:
            # A replay stored this row with no proven owner: its hooks run, and it never mints.
            return SessionStartClaim()
        if _same_process(cli_pid, started, live.parent_pid, live.raw_create_time):
            return proven
        owner_dead = process_is_dead(
            live.parent_pid,
            live.raw_create_time if isinstance(live.raw_create_time, (int, float)) else None,
        )
        if start and owner_dead and _take_over_row(db, live, cli_pid=cli_pid, create_time=started):
            logger.info("%s session %s rebound from its dead CLI", source, external_id)
            return proven
        return _refuse(SESSION_CLAIMANT_MISMATCH, "another CLI owns the row", source, external_id)
    mark = startup_mark(db, machine_id=machine_id, source=source, external_id=external_id)
    pin = None if mark is None else mark.pin
    if pin is not None and _same_process(cli_pid, started, pin.cli_pid, pin.cli_create_time):
        return SessionStartClaim(
            proven=True, cli_pid=cli_pid, create_time=started, creates_row=not start
        )
    if not start:
        if pin is None:
            return _refuse(
                SESSION_STARTUP_UNPINNED, "no startup pinned its CLI", source, external_id
            )
        return _refuse(
            SESSION_CLAIMANT_MISMATCH, "its startup pinned another CLI", source, external_id
        )
    if pin is None:
        return proven
    if process_is_dead(pin.cli_pid, pin.cli_create_time):
        return SessionStartClaim(proven=True, cli_pid=cli_pid, create_time=started, displaced=pin)
    return _refuse(SESSION_CLAIMANT_MISMATCH, "a live CLI holds its pin", source, external_id)


def _claim_shared_host(
    db: Any,
    claimant: Claimant,
    *,
    external_id: str,
    source: str,
    machine_id: str,
    start: bool,
) -> SessionStartClaim:
    """Register a hand-started Codex thread to the live shared worker that runs its hooks.

    The registry, not the row, decides: the row records the seat's TUI, which is the
    worker's client rather than its child. A start registers the thread, which is when it
    is assigned its id and ref, and mints. The first worker to register a thread stands;
    another replaces it only once the registered worker is proven dead, and only on a
    start. Other hooks from a worker the thread is not registered to run without a token.
    A command child of the same worker can name a sibling thread; that stays within the
    operator's own authority (sandboxed seats run with ``--no-daemon``, off this worker).
    """
    if source != "codex" or claimant.parent_pid is None or claimant.create_time is None:
        return SessionStartClaim(refusal=CLAIMANT_UNVERIFIED)
    host = ThreadHost(claimant.parent_pid, claimant.create_time)
    stored = thread_host(db, machine_id=machine_id, external_id=external_id)
    if stored is not None and stored.pid == host.pid:
        if create_time_matches(stored.create_time, host.create_time):
            return SessionStartClaim(proven=start, host=host)
    if stored is None:
        return SessionStartClaim(proven=start, host=host)
    if process_is_dead(stored.pid, stored.create_time):
        return SessionStartClaim(proven=start, host=host, displaced_host=stored)
    return _refuse(
        SESSION_CLAIMANT_MISMATCH, "another live worker registered it", source, external_id
    )
