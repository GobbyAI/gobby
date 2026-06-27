"""Prune dirty-state persistence helpers."""

from __future__ import annotations

from typing import Any

from gobby.code_index.models import CodeIndexPruneDirtyProject
from gobby.storage.hub.protocol import HubDatabase


class CodeIndexPruneStorageMixin:
    """Persistence helpers for projects that need gcode prune retry."""

    db: HubDatabase

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
            rowcount = cursor.rowcount
            return isinstance(rowcount, int) and rowcount > 0

    def list_prune_dirty_projects(
        self,
        limit: int = 100,
        after: tuple[Any, Any, str] | None = None,
    ) -> list[CodeIndexPruneDirtyProject]:
        """List dirty prune roots, oldest first."""
        params: tuple[Any, ...]
        if after is None:
            query = """SELECT project_id, root_path, reason, attempts, last_error, created_at, updated_at
                       FROM code_index_prune_dirty_projects
                       ORDER BY updated_at ASC, created_at ASC, project_id ASC
                       LIMIT %s"""
            params = (limit,)
        else:
            query = """SELECT project_id, root_path, reason, attempts, last_error, created_at, updated_at
                       FROM code_index_prune_dirty_projects
                       WHERE (updated_at, created_at, project_id) > (%s, %s, %s)
                       ORDER BY updated_at ASC, created_at ASC, project_id ASC
                       LIMIT %s"""
            params = (*after, limit)
        rows = self.db.fetchall(query, params)
        return [CodeIndexPruneDirtyProject.from_row(row) for row in rows]
