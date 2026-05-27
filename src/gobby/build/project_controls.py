"""Project-wide build controls."""

from __future__ import annotations

from datetime import UTC, datetime

from gobby.build.project_state import ensure_project_row
from gobby.build.results import BuildControlResult, BuildLifecycleEvent
from gobby.storage.build_history import best_effort_record_event, best_effort_record_run
from gobby.storage.hub.protocol import HubDatabase


def build_stop(
    *,
    db: HubDatabase,
    project_id: str,
) -> BuildControlResult:
    """Stop future dispatcher ticks for the project build queue."""
    return _set_dispatcher_enabled(db=db, project_id=project_id, enabled=False)


def build_resume(
    *,
    db: HubDatabase,
    project_id: str,
) -> BuildControlResult:
    """Resume dispatcher ticks for the project build queue."""
    return _set_dispatcher_enabled(db=db, project_id=project_id, enabled=True)


def _set_dispatcher_enabled(
    *,
    db: HubDatabase,
    project_id: str,
    enabled: bool,
) -> BuildControlResult:
    ensure_project_row(db, project_id)

    event_name = "build_resume" if enabled else "build_stop"
    reason = "gobby build resume" if enabled else "gobby build stop"
    event = _record_project_build_event(
        db,
        project_id=project_id,
        event=event_name,
        reason=reason,
        by_actor="build",
    )
    run = best_effort_record_run(
        db,
        project_id=project_id,
        action=event_name,
        status="completed",
        actor="build",
        summary={"enabled": enabled},
    )
    best_effort_record_event(
        db,
        run_id=run.id if run is not None else None,
        project_id=project_id,
        event_type="project_build_control",
        action=event_name,
        message=reason,
        payload={"enabled": enabled},
    )
    return BuildControlResult(
        project_id=project_id,
        enabled=enabled,
        lifecycle_event=event,
    )


def _record_project_build_event(
    db: HubDatabase,
    *,
    project_id: str,
    event: str,
    reason: str,
    by_actor: str,
) -> BuildLifecycleEvent:
    created_at = datetime.now(UTC).isoformat()
    with db.transaction() as conn:
        row = conn.execute(
            """
            INSERT INTO project_lifecycle_events (project_id, event, reason, by_actor, created_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (project_id, event, reason, by_actor, created_at),
        ).fetchone()
    if row is None:
        raise RuntimeError("Database did not return a project lifecycle event id")
    event_id = int(row["id"])
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
