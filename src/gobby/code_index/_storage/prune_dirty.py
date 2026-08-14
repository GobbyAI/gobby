"""Prune dirty-state persistence helpers."""

from __future__ import annotations

from typing import Any

from gobby.code_index.models import CodeIndexPruneDirtyProject
from gobby.servers.lease_fence import run_hub_mutation
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.utils.machine_id import require_machine_id


class CodeIndexPruneStorageMixin:
    """Persistence helpers for projects that need gcode prune retry."""

    db: HubDatabase

    def mark_prune_dirty(self, project_id: str, root_path: str, reason: str) -> None:
        """Mark a project root as needing gcode prune reconciliation."""
        machine_id = require_machine_id()

        def _write(conn: Transaction) -> None:
            conn.execute(
                """INSERT INTO code_index_prune_dirty_projects (
                    machine_id, project_id, root_path, reason
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT(machine_id, project_id) DO UPDATE SET
                    root_path=excluded.root_path,
                    reason=excluded.reason,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (machine_id, project_id, root_path, reason),
            )

        run_hub_mutation(self.db, _write)

    def record_prune_failure(self, project_id: str, error: str) -> None:
        """Persist a failed prune attempt for later cron retry."""
        machine_id = require_machine_id()

        def _write(conn: Transaction) -> None:
            conn.execute(
                """UPDATE code_index_prune_dirty_projects
                   SET attempts = attempts + 1,
                       last_error = %s,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE machine_id = %s
                   AND project_id = %s""",
                (error, machine_id, project_id),
            )

        run_hub_mutation(self.db, _write)

    def clear_prune_dirty(self, project_id: str) -> bool:
        """Clear a dirty prune marker after prune succeeds."""
        machine_id = require_machine_id()
        cleared = False

        def _write(conn: Transaction) -> None:
            nonlocal cleared
            cursor = conn.execute(
                """DELETE FROM code_index_prune_dirty_projects
                   WHERE machine_id = %s AND project_id = %s""",
                (machine_id, project_id),
            )
            rowcount = cursor.rowcount
            cleared = isinstance(rowcount, int) and rowcount > 0

        run_hub_mutation(self.db, _write)
        return cleared

    def list_prune_dirty_projects(
        self,
        limit: int = 100,
        after: tuple[Any, Any, str] | None = None,
    ) -> list[CodeIndexPruneDirtyProject]:
        """List dirty prune roots, oldest first."""
        machine_id = require_machine_id()
        params: tuple[Any, ...]
        if after is None:
            query = """SELECT machine_id, project_id, root_path, reason, attempts, last_error,
                              created_at, updated_at
                       FROM code_index_prune_dirty_projects
                       WHERE machine_id = %s
                       ORDER BY updated_at ASC, created_at ASC, project_id ASC
                       LIMIT %s"""
            params = (machine_id, limit)
        else:
            query = """SELECT machine_id, project_id, root_path, reason, attempts, last_error,
                              created_at, updated_at
                       FROM code_index_prune_dirty_projects
                       WHERE machine_id = %s
                         AND (updated_at, created_at, project_id) > (%s, %s, %s)
                       ORDER BY updated_at ASC, created_at ASC, project_id ASC
                       LIMIT %s"""
            params = (machine_id, *after, limit)
        rows = self.db.fetchall(query, params)
        return [CodeIndexPruneDirtyProject.from_row(row) for row in rows]
