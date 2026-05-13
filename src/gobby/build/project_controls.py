"""Project-wide build controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from gobby.build.results import BuildControlResult, BuildLifecycleEvent
from gobby.runner import install_dispatcher_cron_row
from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.database import DatabaseProtocol


def build_stop(
    *,
    db: DatabaseProtocol,
    project_id: str,
) -> BuildControlResult:
    """Stop future dispatcher ticks for the project build queue."""
    return _set_dispatcher_enabled(db=db, project_id=project_id, enabled=False)


def build_resume(
    *,
    db: DatabaseProtocol,
    project_id: str,
) -> BuildControlResult:
    """Resume dispatcher ticks for the project build queue."""
    return _set_dispatcher_enabled(db=db, project_id=project_id, enabled=True)


def _set_dispatcher_enabled(
    *,
    db: DatabaseProtocol,
    project_id: str,
    enabled: bool,
) -> BuildControlResult:
    job = install_dispatcher_cron_row(db, project_id=project_id)
    next_run = compute_next_run(replace(job, enabled=True)) if enabled else None
    storage = CronJobStorage(db)
    updated = None
    with db.transaction():
        updated = storage.update_job(job.id, enabled=enabled)
        if updated is None:
            raise RuntimeError(f"Dispatcher cron row disappeared during build control: {job.id}")
        updated = storage.update_system_job_bookkeeping(
            job.id,
            next_run_at=next_run.isoformat() if next_run else None,
        )
        if updated is None:
            raise RuntimeError(f"Dispatcher cron row disappeared during build control: {job.id}")

    event_name = "build_resume" if enabled else "build_stop"
    reason = "gobby build resume" if enabled else "gobby build stop"
    event = _record_project_build_event(
        db,
        project_id=project_id,
        event=event_name,
        reason=reason,
        by_actor="build",
    )
    return BuildControlResult(
        project_id=project_id,
        enabled=updated.enabled,
        cron_job_id=updated.id,
        lifecycle_event=event,
    )


def _record_project_build_event(
    db: DatabaseProtocol,
    *,
    project_id: str,
    event: str,
    reason: str,
    by_actor: str,
) -> BuildLifecycleEvent:
    created_at = datetime.now(UTC).isoformat()
    event_id: int | None = None
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO project_lifecycle_events (project_id, event, reason, by_actor, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, event, reason, by_actor, created_at),
        )
        event_id = cursor.lastrowid
    if event_id is None:
        raise RuntimeError("SQLite did not return a project lifecycle event id")
    return BuildLifecycleEvent(
        id=event_id,
        project_id=project_id,
        event=event,
        reason=reason,
        by_actor=by_actor,
        created_at=created_at,
    )


__all__ = [
    "build_resume",
    "build_stop",
]
