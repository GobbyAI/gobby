"""Project-level build automation state helpers."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase

BUILD_CONTROL_EVENTS = ("build_stop", "build_resume")


def ensure_project_row(db: HubDatabase, project_id: str) -> None:
    """Ensure project-scoped build control rows have a valid project."""
    db.execute(
        """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        (project_id, f"project:{project_id}"),
    )


def is_project_automation_enabled(db: HubDatabase, project_id: str | None) -> bool:
    """Return whether project-wide build automation is currently enabled."""
    if not project_id:
        return True
    row = db.fetchone(
        """
        SELECT event
          FROM project_lifecycle_events
         WHERE project_id = %s
           AND event IN ('build_stop', 'build_resume')
         ORDER BY created_at DESC, id DESC
         LIMIT 1
        """,
        (project_id,),
    )
    return row is None or row["event"] != "build_stop"
