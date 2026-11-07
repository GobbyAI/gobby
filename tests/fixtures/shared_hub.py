"""Bound and name a shared test-hub lock wait on the maintenance connection.

`isolated_test_database` creates and drops its database on the maintenance connection.
PostgreSQL makes those statements wait for a session that holds a conflicting lock on
pg_database, and a stale run can leave such a session open. Without a bound the run
hangs with no output. The bound turns the wait into `SharedHubLockWait`, which names
the lock-holding backend and every host process connected to the hub, each with its
PID, working directory and age, so the holder can be found and ended.

The hub runs in a container. Its view of a client is the bridge address and a proxy
port, so a backend cannot be matched to a host process by port. The report therefore
lists the backend from PostgreSQL and the host connections from `lsof`.

The report reads everything under one monotonic budget, `HUB_DIAGNOSTICS_SECONDS`. When
the budget runs out, the fields it could not read are marked unknown. A wait is then at
most `HUB_LOCK_WAIT_SECONDS` plus `HUB_DIAGNOSTICS_SECONDS`.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg

HUB_LOCK_WAIT_SECONDS = 30
HUB_DIAGNOSTICS_SECONDS = 20
_monotonic = time.monotonic
_COMMAND_TIMEOUT_SECONDS = 10
_CLIENT_SOCKET = re.compile(r"127\.0\.0\.1:\d+->127\.0\.0\.1:(\d+)")

_PG_DATABASE_HOLDERS = """
SELECT activity.pid,
       string_agg(DISTINCT lock.mode, ', ') AS modes,
       (now() - activity.backend_start)::text AS backend_age
FROM pg_locks AS lock
JOIN pg_stat_activity AS activity ON activity.pid = lock.pid
WHERE lock.granted
  AND lock.pid <> pg_backend_pid()
  AND (
    (lock.locktype = 'relation' AND lock.relation = 'pg_catalog.pg_database'::regclass::oid)
    OR (lock.locktype = 'object' AND lock.classid = 'pg_catalog.pg_database'::regclass::oid)
  )
GROUP BY activity.pid, activity.backend_start
ORDER BY activity.pid
"""


class SharedHubLockWait(RuntimeError):
    """A maintenance statement waited on a hub lock another session holds."""


@dataclass
class _Budget:
    """One monotonic deadline shared by every diagnostic read of a single report."""

    deadline: float
    incomplete: bool = False

    def remaining(self) -> float:
        return self.deadline - _monotonic()


@dataclass(frozen=True)
class _HostProcess:
    pid: int
    cwd: str | None
    age: str | None

    def describe(self) -> str:
        return f"pid {self.pid} (cwd {self.cwd or 'unknown'}, age {self.age or 'unknown'})"


def _command_lines(argv: list[str], budget: _Budget) -> list[str] | None:
    """Output lines of one diagnostic command, or None when it could not finish in budget."""
    timeout = min(_COMMAND_TIMEOUT_SECONDS, budget.remaining())
    if timeout <= 0:
        budget.incomplete = True
        return None
    try:
        listing = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        budget.incomplete = True
        return None
    return listing.stdout.splitlines()


def _hub_client_pids(hub_port: int, budget: _Budget) -> list[int] | None:
    """Host PIDs other than this one with an established connection to the hub."""
    lines = _command_lines(
        ["lsof", "-nP", f"-iTCP:{hub_port}", "-sTCP:ESTABLISHED", "-Fpn"], budget
    )
    if lines is None:
        return None
    owners: list[int] = []
    current: int | None = None
    for line in lines:
        if line.startswith("p"):
            current = int(line[1:])
        elif line.startswith("n") and current is not None and current != os.getpid():
            match = _CLIENT_SOCKET.fullmatch(line[1:])
            if match and int(match.group(1)) == hub_port and current not in owners:
                owners.append(current)
    return owners


def _host_processes(pids: list[int], budget: _Budget) -> list[_HostProcess]:
    """Cwd and age of every PID, from one lsof call and one ps call."""
    if not pids:
        return []
    pid_list = ",".join(str(pid) for pid in pids)
    cwds: dict[int, str] = {}
    current: int | None = None
    for line in _command_lines(["lsof", "-a", "-p", pid_list, "-d", "cwd", "-Fpn"], budget) or []:
        if line.startswith("p"):
            current = int(line[1:])
        elif line.startswith("n") and current is not None:
            cwds.setdefault(current, line[1:])
    ages: dict[int, str] = {}
    for line in _command_lines(["ps", "-o", "pid=,etime=", "-p", pid_list], budget) or []:
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit():
            ages[int(fields[0])] = fields[1]
    return [_HostProcess(pid, cwds.get(pid), ages.get(pid)) for pid in pids]


def _backend_holders(conn: psycopg.Connection[Any], budget: _Budget) -> str:
    """Backends holding pg_database, queried under the budget's remaining statement deadline."""
    remaining_ms = int(budget.remaining() * 1000)
    if remaining_ms <= 0:
        budget.incomplete = True
        return "unknown (diagnostics budget used up)"
    try:
        conn.execute("SELECT set_config('statement_timeout', %s, false)", (f"{remaining_ms}ms",))
        rows = conn.execute(_PG_DATABASE_HOLDERS).fetchall()
    except psycopg.Error as exc:
        # A failed diagnostic must not replace the lock failure it reports.
        budget.incomplete = True
        return f"unknown ({type(exc).__name__})"
    backends = [
        f"backend pid {pid} ({modes} on pg_database, backend age {age})" for pid, modes, age in rows
    ]
    return "; ".join(backends) or "none still visible"


def _describe_holders(conn: psycopg.Connection[Any]) -> str:
    """Name the lock holders; every diagnostic read shares one budget."""
    budget = _Budget(deadline=_monotonic() + HUB_DIAGNOSTICS_SECONDS)
    backends = _backend_holders(conn, budget)
    hub_port = int(conn.info.port or 0)
    owners = _hub_client_pids(hub_port, budget)
    if owners is None:
        processes = "unknown"
    else:
        described = [process.describe() for process in _host_processes(owners, budget)]
        processes = "; ".join(described) or "none listed"
    report = (
        f"Lock holders: {backends}. Host processes connected to 127.0.0.1:{hub_port}: {processes}."
    )
    if budget.incomplete:
        report += (
            " Fields marked unknown could not be read: the "
            f"{HUB_DIAGNOSTICS_SECONDS} s diagnostics budget ran out or a tool failed."
        )
    return report


@contextlib.contextmanager
def shared_hub_lock_waits(conn: psycopg.Connection[Any], operation: str) -> Iterator[None]:
    """Fail a maintenance lock wait after HUB_LOCK_WAIT_SECONDS, naming the holders."""
    bound = f"{HUB_LOCK_WAIT_SECONDS}s"
    conn.execute("SELECT set_config('lock_timeout', %s, false)", (bound,))
    try:
        yield
    except psycopg.errors.LockNotAvailable as exc:
        raise SharedHubLockWait(
            f"{operation} on the test hub waited {bound} for a lock held by another session. "
            f"{_describe_holders(conn)}"
        ) from exc
