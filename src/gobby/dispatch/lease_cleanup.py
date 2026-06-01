"""Dispatcher lease cleanup helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gobby.dispatch.constants import DISPATCH_HOLDER, ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager


def sweep_expired_leases(storage: TaskDispatchMutexManager) -> int:
    active_run_ids = {run.id for run in LocalAgentRunManager(storage.db).list_active(limit=1000)}
    rows = storage.db.fetchall(
        """
        SELECT task_id, run_id
          FROM task_dispatch_mutex
         WHERE lease_until IS NOT NULL
           AND lease_until < %s
        """,
        (datetime.now(UTC).isoformat(),),
    )
    cleared = 0
    for row in rows:
        if row["run_id"] in active_run_ids:
            continue
        if storage.force_release(row["task_id"]):
            cleared += 1
    return cleared


def sweep_orphan_no_run_dispatch_mutexes(
    mutex_storage: TaskDispatchMutexManager,
    db: HubDatabase,
    *,
    project_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Release dispatcher leases that never attached a run and aged past the grace window."""
    resolved_now = now or datetime.now(UTC)
    project_join = ""
    project_filter = ""
    params: list[object] = [DISPATCH_HOLDER]
    if project_id is not None:
        project_join = "JOIN tasks t ON t.id = mutex.task_id"
        project_filter = "AND t.project_id = %s"
        params.append(project_id)
    rows = db.fetchall(
        f"""
        SELECT mutex.task_id, mutex.lease_until, mutex.updated_at
          FROM task_dispatch_mutex mutex
          {project_join}
         WHERE mutex.lease_holder = %s
           AND mutex.run_id IS NULL
           {project_filter}
        """,  # nosec B608 # project join/filter are fixed SQL fragments selected above.
        tuple(params),
    )
    cleared = 0
    for row in rows:
        lease_until = _parse_mutex_timestamp(row["lease_until"])
        if lease_until is not None:
            if lease_until >= resolved_now:
                continue
            should_release = True
        else:
            should_release = False

        updated_at = _parse_mutex_timestamp(row["updated_at"])
        if updated_at is None and not should_release:
            continue
        if (
            not should_release
            and updated_at is not None
            and resolved_now - updated_at < timedelta(seconds=ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS)
        ):
            continue
        if _release_orphan_no_run_mutex(
            mutex_storage,
            task_id=str(row["task_id"]),
            updated_at=str(row["updated_at"]),
        ):
            cleared += 1
    return cleared


def _release_orphan_no_run_mutex(
    mutex_storage: TaskDispatchMutexManager,
    *,
    task_id: str,
    updated_at: str,
) -> bool:
    with mutex_storage.db.transaction() as conn:
        cursor = conn.execute(
            """
            DELETE FROM task_dispatch_mutex
             WHERE task_id = %s
               AND lease_holder = %s
               AND run_id IS NULL
               AND updated_at = %s
            """,
            (task_id, DISPATCH_HOLDER, updated_at),
        )
        return cursor.rowcount > 0


def _parse_mutex_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = [
    "sweep_expired_leases",
    "sweep_orphan_no_run_dispatch_mutexes",
]
