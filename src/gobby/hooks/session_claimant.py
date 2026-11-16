"""Kernel-attested claimant of a machine-bearer hook (#25905).

The machine-wide bearer names no session, so a hook that would create a session's row or
mint its credential must come from the CLI process that owns that session. The daemon
reads the request's local TCP socket from the kernel socket table: its owner is the hook
process, and the stabilized parent of that process is the CLI, named by pid and start
time. The body's own parent pid is never proof.

Ownership is durable. A live terminal row stores its CLI, and so does a row that expired
while its session sat idle: that CLI's next hook revives it unless the CLI has moved to
another session. A deferred startup's mark pins its CLI, and only that CLI's hook creates
the row (session_startup_marks). A thread run by Codex's shared app-server is registered
to that worker (codex_thread_hosts). The first
owner stands: another may replace it only after proving it dead, by pid and start time,
and the replacement is a compare-and-swap, so of two racers one wins. A Codex CLI's
proven start holds its thread in that registry from its claim to its settle, so no
worker child registers or mints the thread while the CLI's handler revives its row.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import psutil

from gobby.hooks.terminal_context import _is_codex_shared_host, _stable_cli_process
from gobby.sessions.terminal_kill import create_time_matches
from gobby.storage.codex_thread_hosts import (
    ThreadHost,
    hold_thread_for_cli,
    refresh_thread_host,
    register_thread_host,
    thread_host,
    unregister_thread_host,
)
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

    ``proven`` hooks run with their CLI stamped as the row's owner, and mint once a live
    row stores that CLI. ``displaced`` is the dead pin a start replaces; ``host`` is the
    shared worker a Codex thread is registered to.
    """

    refusal: str | None = None
    proven: bool = False
    cli_pid: int | None = None
    create_time: float | None = None
    displaced: StartupPin | None = None
    host: ThreadHost | None = None

    @property
    def pin(self) -> StartupPin | None:
        """The proven CLI as a startup pin."""
        if self.cli_pid is None or self.create_time is None:
            return None
        return StartupPin(self.cli_pid, self.create_time)


@dataclass(frozen=True)
class _TerminalRow:
    """A terminal session row and the CLI process it stores as owner."""

    session_id: str
    parent_pid: int | None
    raw_create_time: Any

    @classmethod
    def from_row(cls, row: Any) -> _TerminalRow:
        context = parse_terminal_context_value(row["terminal_context"]) or {}
        parent_pid = context.get("parent_pid")
        if isinstance(parent_pid, bool) or not isinstance(parent_pid, int):
            parent_pid = None
        return cls(
            session_id=str(row["id"]),
            parent_pid=parent_pid,
            raw_create_time=context.get("parent_create_time"),
        )


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
    """A pid is reused, so only the pid together with its start time names a process."""
    return (
        pid == other
        and create_time is not None
        and isinstance(other_time, (int, float))
        and not isinstance(other_time, bool)
        and create_time_matches(create_time, float(other_time))
    )


def _live_terminal(
    db: Any, *, external_id: str, source: str, machine_id: str
) -> _TerminalRow | None:
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
    return None if row is None else _TerminalRow.from_row(row)


def _own_expired_terminal(
    db: Any, cli_pid: int, started: float, *, external_id: str, source: str, machine_id: str
) -> str | None:
    """The id of the thread's expired row, when this CLI may bring it back.

    The thread's newest row must be expired, whatever expired it, and store this CLI
    process as owner. A CLI holds one conversation, so a live row that stores the same
    process shows it left this one, by a clear or by resuming another thread.
    """
    row = db.fetchone(
        """
        SELECT id, status, terminal_context FROM sessions
        WHERE external_id = %s AND source = %s AND machine_id = %s
          AND session_type = 'terminal' AND status <> 'deleted'
        ORDER BY created_at DESC, id
        LIMIT 1
        """,
        (external_id, source, machine_id),
    )
    if row is None or row["status"] != "expired":
        return None

    def stores_this_cli(terminal: _TerminalRow) -> bool:
        return terminal.parent_pid is not None and _same_process(
            cli_pid, started, terminal.parent_pid, terminal.raw_create_time
        )

    expired = _TerminalRow.from_row(row)
    if not stores_this_cli(expired):
        return None
    live_rows = db.fetchall(
        """
        SELECT id, terminal_context FROM sessions
        WHERE machine_id = %s AND session_type = 'terminal' AND status = ANY(%s)
        """,
        (machine_id, list(LIVE_SESSION_STATUSES)),
    )
    if any(stores_this_cli(_TerminalRow.from_row(held)) for held in live_rows):
        return None
    return expired.session_id


def _take_over_row(db: Any, live: _TerminalRow, *, cli_pid: int, create_time: float) -> bool:
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
    revive: Callable[[str], bool] | None = None,
) -> SessionStartClaim:
    """Decide what a machine-bearer hook may do for its kernel-attested claimant.

    A daemon-internal replay proves nothing and mints nothing. An unresolved claimant is
    refused. A live row's owner proceeds; a start from a new CLI takes over a row whose
    owner is proven dead; anyone else is refused. With no live row, a start from any CLI
    may pin the thread unless a live CLI already holds the pin, and only the pinned CLI's
    hook may create the row. A hook with no pin creates nothing.

    ``revive`` brings an expired row back by its session id and reports whether it is
    live again. With it, a hook from the CLI process an expired row stores as owner is
    decided as a live row's hook once the row is back, unless that CLI holds another
    live row; without it, or when revival declines, the hook is refused as any hook
    with no pin is.
    """
    if claimant is None:
        return SessionStartClaim(refusal=CLAIMANT_UNVERIFIED)
    if claimant.pid == daemon_pid:
        return SessionStartClaim()
    key = {"external_id": external_id, "source": source, "machine_id": machine_id}
    if claimant.shared_host:
        return _claim_shared_host(db, claimant, start=start, **key)
    claim = _claim_cli(db, claimant, start=start, revive=revive, **key)
    if start and claim.proven and source == "codex" and claim.cli_pid and claim.create_time:
        # Hold the thread before the handler runs: a resume or take-over revives the row
        # before settle drops any worker registration, and a worker child must not mint
        # it in between. Settle drops the hold; a handler that raises leaves it, which
        # refuses worker children until a start displaces this CLI once it is dead.
        hold_thread_for_cli(
            db,
            machine_id=machine_id,
            external_id=external_id,
            cli=ThreadHost(claim.cli_pid, claim.create_time),
        )
    return claim


def _claim_cli(
    db: Any,
    claimant: Claimant,
    *,
    external_id: str,
    source: str,
    machine_id: str,
    start: bool,
    revive: Callable[[str], bool] | None,
) -> SessionStartClaim:
    """Decide a hook whose claimant is a CLI process rather than a shared worker."""
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
        return proven
    if not start:
        if pin is None:
            key = {"external_id": external_id, "source": source, "machine_id": machine_id}
            expired = None if revive is None else _own_expired_terminal(db, cli_pid, started, **key)
            if revive is not None and expired is not None and revive(expired):
                # An idle row expired while its CLI never left, and that CLI's hook
                # brought it back. Another CLI's start may have registered the row
                # meanwhile, so the hook is decided again from the row as it now stands.
                logger.info("%s session %s revived on a hook from its CLI", source, external_id)
                return _claim_cli(db, claimant, start=start, revive=None, **key)
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

    A thread a CLI runs its own hooks for (a ``--no-daemon`` seat) is never the worker's:
    its start pinned that CLI, and its row was created without a registration. A child of
    the shared worker naming such a thread is refused while the pinned CLI lives, and a
    live row the thread was never registered for is refused too, so the child neither
    rewrites the row nor mints for it. The row records the seat's TUI, which is the
    worker's client rather than its child, so the registry decides for the worker's own
    threads. A start registers the thread, which is when it is assigned its id and ref
    (before its deferred row exists), and mints. The first worker to register a thread
    stands; another replaces it only once the registered worker is proven dead, and only
    on a start. Other hooks from a worker the thread is not registered to run without a
    token. A command child of the same worker can name a sibling thread registered to
    that worker; that stays within the operator's own authority (#25911).
    """
    if source != "codex" or claimant.parent_pid is None or claimant.create_time is None:
        return SessionStartClaim(refusal=CLAIMANT_UNVERIFIED)
    host = ThreadHost(claimant.parent_pid, claimant.create_time)
    mark = startup_mark(db, machine_id=machine_id, source=source, external_id=external_id)
    pin = None if mark is None else mark.pin
    if pin is not None and not process_is_dead(pin.cli_pid, pin.cli_create_time):
        return _refuse(SESSION_CLAIMANT_MISMATCH, "a live CLI pinned it", source, external_id)
    key = {"external_id": external_id, "source": source, "machine_id": machine_id}
    stored = thread_host(db, machine_id=machine_id, external_id=external_id)
    displaced: ThreadHost | None = None
    if (
        stored is not None
        and stored.pid == host.pid
        and create_time_matches(stored.create_time, host.create_time)
    ):
        # The registered worker itself, its start time stored at the precision it was read.
        host = stored
    if stored is None:
        if _live_terminal(db, **key):
            return _refuse(
                SESSION_CLAIMANT_MISMATCH,
                "its live row was never registered to a worker",
                source,
                external_id,
            )
    elif stored != host:
        if not process_is_dead(stored.pid, stored.create_time):
            return _refuse(
                SESSION_CLAIMANT_MISMATCH,
                "another live worker or CLI holds it",
                source,
                external_id,
            )
        displaced = stored
    if not start:
        return SessionStartClaim(host=host)
    return _register_shared_host(db, host, displaced=displaced, new=stored is None, **key)


def _register_shared_host(
    db: Any,
    host: ThreadHost,
    *,
    displaced: ThreadHost | None,
    new: bool,
    external_id: str,
    source: str,
    machine_id: str,
) -> SessionStartClaim:
    """Register a start's thread before its handler runs, then check again what it wrote.

    The checks above ran before this write, so a ``--no-daemon`` seat may have pinned the
    thread, or pinned it and created its row, in between. The seat's start drops any
    registration after it pins, so a write that lands first is removed; one that lands
    later finds the live pin, or the seat's row on a thread that had no registration, and
    withdraws. A worker the thread was already registered to only refreshes that entry,
    so a registration a seat dropped since the read above stays dropped. Settle writes
    nothing, so no registration appears after these checks.
    """
    thread = {"machine_id": machine_id, "external_id": external_id}
    if new or displaced is not None:
        registered = register_thread_host(db, **thread, host=host, displaced=displaced)
    else:
        registered = refresh_thread_host(db, **thread, host=host)
    if not registered:
        return _refuse(
            SESSION_CLAIMANT_MISMATCH, "another worker registered it first", source, external_id
        )
    mark = startup_mark(db, machine_id=machine_id, source=source, external_id=external_id)
    pin = None if mark is None else mark.pin
    seat_pinned = pin is not None and not process_is_dead(pin.cli_pid, pin.cli_create_time)
    seat_row = new and _live_terminal(
        db, external_id=external_id, source=source, machine_id=machine_id
    )
    if seat_pinned or seat_row:
        unregister_thread_host(db, machine_id=machine_id, external_id=external_id, host=host)
        return _refuse(
            SESSION_CLAIMANT_MISMATCH, "a CLI took the thread meanwhile", source, external_id
        )
    return SessionStartClaim(proven=True, host=host)
