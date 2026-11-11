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

# A registration stays while the worker's hooks keep refreshing it; a thread idle this
# long registers again on its next hook.
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
        db.execute(
            """
            UPDATE codex_thread_hosts SET seen_at = clock_timestamp()
            WHERE machine_id = %s AND external_id = %s
              AND host_pid = %s AND host_create_time = %s
            """,
            (machine_id, external_id, host.pid, host.create_time),
        )
    return thread_host(db, machine_id=machine_id, external_id=external_id) == host


def prune_thread_hosts(db: HubDatabase, *, max_age_hours: int = THREAD_HOST_MAX_AGE_HOURS) -> int:
    """Delete registrations no hook has refreshed for ``max_age_hours``."""
    cursor = db.execute(
        """
        DELETE FROM codex_thread_hosts
        WHERE seen_at < clock_timestamp() - make_interval(hours => %s)
        """,
        (max_age_hours,),
    )
    return cursor.rowcount
