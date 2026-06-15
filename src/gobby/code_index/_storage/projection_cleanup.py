"""Projection cleanup retry state helpers."""

from __future__ import annotations

from gobby.code_index.models import ProjectionCleanupPending, ProjectionCleanupStore
from gobby.storage.hub.protocol import HubDatabase


class CodeIndexProjectionCleanupStorageMixin:
    """Storage methods for pending projection cleanup retries."""

    db: HubDatabase

    def record_projection_cleanup_failure(
        self,
        project_id: str,
        store: ProjectionCleanupStore,
        error: str,
    ) -> None:
        """Persist a failed projection cleanup attempt for maintenance retry."""
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO code_index_projection_cleanup_pending (
                    project_id, store, attempts, last_error
                ) VALUES (%s, %s, 1, %s)
                ON CONFLICT(project_id, store) DO UPDATE SET
                    attempts=code_index_projection_cleanup_pending.attempts + 1,
                    last_error=excluded.last_error,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (project_id, store, error),
            )

    def clear_projection_cleanup_pending(
        self,
        project_id: str,
        store: ProjectionCleanupStore,
    ) -> bool:
        """Clear a pending projection cleanup marker after cleanup succeeds."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """DELETE FROM code_index_projection_cleanup_pending
                   WHERE project_id = %s AND store = %s""",
                (project_id, store),
            )
            return cursor.rowcount > 0

    def list_projection_cleanup_pending(
        self,
        limit: int = 100,
    ) -> list[ProjectionCleanupPending]:
        """List pending projection cleanup retries, oldest first."""
        rows = self.db.fetchall(
            """SELECT project_id, store, attempts, last_error, created_at, updated_at
               FROM code_index_projection_cleanup_pending
               ORDER BY updated_at ASC, created_at ASC
               LIMIT %s""",
            (limit,),
        )
        return [ProjectionCleanupPending.from_row(row) for row in rows]
