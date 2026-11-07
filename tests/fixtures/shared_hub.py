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
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg

HUB_LOCK_WAIT_SECONDS = 30
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


@dataclass(frozen=True)
class _HostProcess:
    pid: int
    cwd: str | None
    age: str | None

    def describe(self) -> str:
        return f"pid {self.pid} (cwd {self.cwd or 'unknown'}, age {self.age or 'unknown'})"


def _command_lines(argv: list[str]) -> list[str]:
    try:
        listing = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return listing.stdout.splitlines()


def _host_process(pid: int) -> _HostProcess:
    cwd = next(
        (
            line[1:]
            for line in _command_lines(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
            if line.startswith("n")
        ),
        None,
    )
    age = next(iter(_command_lines(["ps", "-o", "etime=", "-p", str(pid)])), "").strip()
    return _HostProcess(pid, cwd, age or None)


def _hub_client_processes(hub_port: int) -> list[_HostProcess]:
    """Host processes other than this one with an established connection to the hub."""
    owners: list[int] = []
    current: int | None = None
    for line in _command_lines(["lsof", "-nP", f"-iTCP:{hub_port}", "-sTCP:ESTABLISHED", "-Fpn"]):
        if line.startswith("p"):
            current = int(line[1:])
        elif line.startswith("n") and current is not None and current != os.getpid():
            match = _CLIENT_SOCKET.fullmatch(line[1:])
            if match and int(match.group(1)) == hub_port and current not in owners:
                owners.append(current)
    return [_host_process(pid) for pid in owners]


def _describe_holders(conn: psycopg.Connection[Any]) -> str:
    backend_rows = conn.execute(_PG_DATABASE_HOLDERS).fetchall()
    backends = [
        f"backend pid {pid} ({modes} on pg_database, backend age {age})"
        for pid, modes, age in backend_rows
    ]
    hub_port = int(conn.info.port or 0)
    processes = [process.describe() for process in _hub_client_processes(hub_port)]
    return (
        f"Lock holders: {'; '.join(backends) or 'none still visible'}. "
        f"Host processes connected to 127.0.0.1:{hub_port}: {'; '.join(processes) or 'none listed'}."
    )


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
