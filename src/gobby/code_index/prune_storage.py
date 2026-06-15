"""Storage helpers for code-index prune reconciliation state."""

from __future__ import annotations

from typing import Any

from gobby.code_index.models import CodeIndexPruneDirtyProject


class CodeIndexPruneStorageMixin:
    """Persistence helpers for projects that need gcode prune retry."""

    db: Any

    def mark_prune_dirty(self, project_id: str, root_path: str, reason: str) -> None:
        """Mark a project root as needing gcode prune reconciliation."""
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO code_index_prune_dirty_projects (
                    project_id, root_path, reason
                ) VALUES (%s, %s, %s)
                ON CONFLICT(project_id) DO UPDATE SET
                    root_path=excluded.root_path,
                    reason=excluded.reason,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (project_id, root_path, reason),
            )

    def record_prune_failure(self, project_id: str, error: str) -> None:
        """Persist a failed prune attempt for later cron retry."""
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE code_index_prune_dirty_projects
                   SET attempts = attempts + 1,
                       last_error = %s,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE project_id = %s""",
                (error, project_id),
            )

    def clear_prune_dirty(self, project_id: str) -> bool:
        """Clear a dirty prune marker after prune succeeds."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM code_index_prune_dirty_projects WHERE project_id = %s",
                (project_id,),
            )
            return cursor.rowcount > 0

    def list_prune_dirty_projects(self, limit: int = 100) -> list[CodeIndexPruneDirtyProject]:
        """List dirty prune roots, oldest first."""
        rows = self.db.fetchall(
            """SELECT project_id, root_path, reason, attempts, last_error, created_at, updated_at
               FROM code_index_prune_dirty_projects
               ORDER BY updated_at ASC, created_at ASC
               LIMIT %s""",
            (limit,),
        )
        return [CodeIndexPruneDirtyProject.from_row(row) for row in rows]
