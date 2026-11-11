"""The shared Codex app-server worker each hand-started thread registered from (#25905).

Codex's managed app-server runs every hand-started thread's hooks and gmcp under one
worker process, so no CLI process names a single thread. A hook whose kernel-attested
parent is that worker registers the thread to it, by pid and start time. gmcp under the
same worker may then bind a call to the thread its host names, and only while that live
worker is the one recorded. The first worker to register a thread stands; another
replaces it only after proving it dead and naming it exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import LIVE_SESSION_STATUSES

# A registration whose thread has no live row and no start has refreshed for this long
# is pruned. One with a live row stays: a live row with no registration is refused.
THREAD_HOST_MAX_AGE_HOURS = 7 * 24


@dataclass(frozen=True)
class ThreadHost:
    """A shared app-server worker: its pid and that process's start time."""

    pid: int
    create_time: float


def thread_host(db: HubDatabase, *, machine_id: str, external_id: str) -> ThreadHost | None:
    """The worker the thread registered from, if it is registered."""
    row = db.fetchone(
        """
        SELECT host_pid, host_create_time FROM codex_thread_hosts
        WHERE machine_id = %s AND external_id = %s
        """,
        (machine_id, external_id),
    )
    if row is None:
        return None
    return ThreadHost(int(row["host_pid"]), float(row["host_create_time"]))


def register_thread_host(
    db: HubDatabase,
    *,
    machine_id: str,
    external_id: str,
    host: ThreadHost,
    displaced: ThreadHost | None = None,
) -> bool:
    """Register ``host`` for the thread; return whether the thread is now registered to it.

    Without ``displaced`` the first registration wins and a later one from the same worker
    refreshes it. With it, the stored worker is replaced only if it is exactly
    ``displaced``, a worker the caller proved dead.
    """
    if displaced is not None:
        db.execute(
            """
            UPDATE codex_thread_hosts
            SET host_pid = %s, host_create_time = %s, seen_at = clock_timestamp()
            WHERE machine_id = %s AND external_id = %s
              AND host_pid = %s AND host_create_time = %s
            """,
            (
                host.pid,
                host.create_time,
                machine_id,
                external_id,
                displaced.pid,
                displaced.create_time,
            ),
        )
    else:
        db.execute(
            """
            INSERT INTO codex_thread_hosts (machine_id, external_id, host_pid, host_create_time)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (machine_id, external_id, host.pid, host.create_time),
        )
    return refresh_thread_host(db, machine_id=machine_id, external_id=external_id, host=host)


def refresh_thread_host(
    db: HubDatabase, *, machine_id: str, external_id: str, host: ThreadHost
) -> bool:
    """Refresh ``host``'s existing registration; return whether the thread is registered to it.

    It never creates one, so a registration deleted since the caller read it stays deleted.
    """
    db.execute(
        """
        UPDATE codex_thread_hosts SET seen_at = clock_timestamp()
        WHERE machine_id = %s AND external_id = %s
          AND host_pid = %s AND host_create_time = %s
        """,
        (machine_id, external_id, host.pid, host.create_time),
    )
    return thread_host(db, machine_id=machine_id, external_id=external_id) == host


def hold_thread_for_cli(
    db: HubDatabase, *, machine_id: str, external_id: str, cli: ThreadHost
) -> None:
    """Replace any registration with ``cli``, a CLI proven to run the thread's own hooks.

    A ``--no-daemon`` seat's start claim holds its thread this way before its handler
    runs, so no worker refresh passes and no worker child registers while the hold stands.
    Its settle then drops the entry.
    """
    db.execute(
        """
        INSERT INTO codex_thread_hosts (machine_id, external_id, host_pid, host_create_time)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (machine_id, external_id) DO UPDATE
        SET host_pid = EXCLUDED.host_pid,
            host_create_time = EXCLUDED.host_create_time,
            seen_at = clock_timestamp()
        """,
        (machine_id, external_id, cli.pid, cli.create_time),
    )


def unregister_thread_host(
    db: HubDatabase, *, machine_id: str, external_id: str, host: ThreadHost | None = None
) -> None:
    """Forget the thread's worker, or only ``host`` when it is the one registered.

    A CLI that proved it runs the thread's hooks itself drops any worker. A worker that
    finds a live CLI pinned the thread after it registered withdraws only its own entry.
    """
    if host is None:
        db.execute(
            "DELETE FROM codex_thread_hosts WHERE machine_id = %s AND external_id = %s",
            (machine_id, external_id),
        )
        return
    db.execute(
        """
        DELETE FROM codex_thread_hosts
        WHERE machine_id = %s AND external_id = %s AND host_pid = %s AND host_create_time = %s
        """,
        (machine_id, external_id, host.pid, host.create_time),
    )


def prune_thread_hosts(db: HubDatabase, *, max_age_hours: int = THREAD_HOST_MAX_AGE_HOURS) -> int:
    """Delete registrations unrefreshed for ``max_age_hours`` whose thread has no live row."""
    cursor = db.execute(
        """
        DELETE FROM codex_thread_hosts h
        WHERE h.seen_at < clock_timestamp() - make_interval(hours => %s)
          AND NOT EXISTS (
            SELECT 1 FROM sessions s
            WHERE s.machine_id::text = h.machine_id::text
              AND s.external_id::text = h.external_id::text
              AND s.source = 'codex' AND s.session_type = 'terminal'
              AND s.status = ANY(%s)
          )
        """,
        (max_age_hours, list(LIVE_SESSION_STATUSES)),
    )
    return cursor.rowcount
