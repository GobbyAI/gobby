"""Kernel-attested claimant of a machine-bearer SessionStart.

The machine-wide bearer names no session, so a SessionStart that would mint a session
credential must come from the CLI process that owns that session. The daemon reads the
local TCP socket of the request from the kernel socket table: its owner is the hook
process, and the stabilized parent of that process is the CLI. A session with a live
terminal row keeps its stored CLI pid, and a claimant with another CLI pid is refused
before the handler runs. A new session stores the proven CLI pid. The body's own
parent pid is never proof.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psutil

from gobby.hooks.terminal_context import _is_codex_shared_host, _stable_cli_process
from gobby.storage.sessions import LIVE_SESSION_STATUSES
from gobby.terminal_context import parse_terminal_context_value

logger = logging.getLogger(__name__)

SESSION_CLAIMANT_MISMATCH = "session_claimant_mismatch"
CLAIMANT_UNVERIFIED = "claimant_unverified"

_PROCESS_ERRORS = (psutil.Error, OSError)


@dataclass(frozen=True)
class Claimant:
    """The process that opened a request's TCP socket, with its proven CLI pid.

    ``cli_pid`` is None when no single session can be attested through the parent: it is a
    shared host that serves several sessions (``shared_host``), or it has no parent or
    cannot be inspected.
    """

    pid: int
    parent_pid: int | None
    cli_pid: int | None
    shared_host: bool = False


@dataclass(frozen=True)
class SessionStartClaim:
    """Decision for a machine-bearer SessionStart: refuse, mint, or run without a token."""

    refusal: str | None = None
    proven: bool = False
    cli_pid: int | None = None


@dataclass(frozen=True)
class _LiveTerminal:
    parent_pid: int | None


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
    """Resolve the CLI behind a front-door hook from the peer port gdaemon observed.

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
    try:
        parent = psutil.Process(pid).parent()
        if parent is None:
            return Claimant(pid=pid, parent_pid=None, cli_pid=None)
        if _is_codex_shared_host(parent):
            return Claimant(pid=pid, parent_pid=parent.pid, cli_pid=None, shared_host=True)
        cli = _stable_cli_process(parent)
    except _PROCESS_ERRORS:
        return Claimant(pid=pid, parent_pid=None, cli_pid=None)
    return Claimant(pid=pid, parent_pid=parent.pid, cli_pid=cli.pid)


def _live_terminal(
    db: Any, *, external_id: str, source: str, machine_id: str
) -> _LiveTerminal | None:
    row = db.fetchone(
        """
        SELECT terminal_context FROM sessions
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
    return _LiveTerminal(parent_pid=parent_pid)


def claim_session_start(
    db: Any,
    *,
    claimant: Claimant | None,
    daemon_pid: int,
    external_id: str,
    source: str,
    machine_id: str,
) -> SessionStartClaim:
    """Decide what a machine-bearer SessionStart may do for its kernel-attested claimant.

    A daemon-internal replay has no proof and mints nothing. An unresolved claimant is
    refused. An existing session is refused unless its stored CLI pid is the claimant's.
    A new session is proven by its claimant's CLI pid, which the handler records. That is
    the stabilized pid the live check compares, not the raw parent of a Droid exec backend.
    """
    if claimant is None:
        return SessionStartClaim(refusal=CLAIMANT_UNVERIFIED)
    if claimant.pid == daemon_pid:
        return SessionStartClaim()
    live = _live_terminal(db, external_id=external_id, source=source, machine_id=machine_id)
    if claimant.cli_pid is None:
        # No single CLI attests this start: never mint, and never touch a live row.
        if live:
            return SessionStartClaim(refusal=CLAIMANT_UNVERIFIED)
        reason = (
            "its hook runs under a shared host that cannot attest one session"
            if claimant.shared_host
            else "its hook's parent process is missing or could not be inspected"
        )
        logger.warning(
            "SessionStart for %s session %s mints no session credential: %s (parent pid %s)",
            source,
            external_id,
            reason,
            claimant.parent_pid,
        )
        return SessionStartClaim()
    if live is None:
        return SessionStartClaim(proven=True, cli_pid=claimant.cli_pid)
    if live.parent_pid != claimant.cli_pid:
        logger.warning("Machine SessionStart claimant does not own the live session")
        return SessionStartClaim(refusal=SESSION_CLAIMANT_MISMATCH)
    return SessionStartClaim(proven=True)
