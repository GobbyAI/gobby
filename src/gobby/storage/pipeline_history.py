"""Project-scoped pipeline execution history cleanup."""

from __future__ import annotations

from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.pipeline_state import ExecutionStatus

_TERMINAL_STATUSES = frozenset(
    {
        ExecutionStatus.COMPLETED.value,
        ExecutionStatus.FAILED.value,
        ExecutionStatus.CANCELLED.value,
    }
)


def _summarize_history(rows: list[Any], pipeline_name: str, project_id: str) -> dict[str, Any]:
    matching_rows = [row for row in rows if row["pipeline_name"] == pipeline_name]
    blockers = [row for row in rows if row["status"] not in _TERMINAL_STATUSES]
    status_counts: dict[str, int] = {}
    selected_status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"])
        selected_status_counts[status] = selected_status_counts.get(status, 0) + 1
        if row["pipeline_name"] == pipeline_name:
            status_counts[status] = status_counts.get(status, 0) + 1

    matching_count = len(matching_rows)
    terminal_count = sum(row["status"] in _TERMINAL_STATUSES for row in matching_rows)
    return {
        "pipeline_name": pipeline_name,
        "project_id": project_id,
        "matching_count": matching_count,
        "terminal_count": terminal_count,
        "selected_count": len(rows),
        "descendant_count": len(rows) - matching_count,
        "status_counts": status_counts,
        "selected_status_counts": selected_status_counts,
        "blocking_count": len(blockers),
        "blockers": [
            {
                "id": str(row["id"]),
                "pipeline_name": str(row["pipeline_name"]),
                "status": str(row["status"]),
            }
            for row in blockers
        ],
        "can_clear": terminal_count > 0 and not blockers,
    }


class PipelineHistoryStorageMixin:
    """Preview and clear one pipeline's execution history."""

    db: HubDatabase
    project_id: str | None

    def _history_project_id(self) -> str:
        if not self.project_id:
            raise ValueError("Pipeline execution history cleanup requires a project scope")
        return self.project_id

    @staticmethod
    def _history_query(*, for_update: bool = False) -> str:
        lock_clause = " FOR UPDATE OF pe" if for_update else ""
        return f"""
            WITH RECURSIVE selected AS (
                SELECT id
                FROM pipeline_executions
                WHERE project_id = %s AND pipeline_name = %s
                UNION
                SELECT child.id
                FROM pipeline_executions child
                JOIN selected parent ON child.parent_execution_id = parent.id
                WHERE child.project_id = %s
            )
            SELECT pe.id, pe.pipeline_name, pe.status, pe.parent_execution_id
            FROM pipeline_executions pe
            JOIN selected ON selected.id = pe.id
            WHERE pe.project_id = %s
            ORDER BY pe.created_at, pe.id{lock_clause}
        """

    def preview_pipeline_execution_history(self, pipeline_name: str) -> dict[str, Any]:
        """Return project-scoped matching, descendant, and blocker counts."""
        name = pipeline_name.strip()
        if not name:
            raise ValueError("pipeline_name must not be empty")
        project_id = self._history_project_id()
        params = (project_id, name, project_id, project_id)
        rows = self.db.fetchall(self._history_query(), params)
        summary = _summarize_history(rows, name, project_id)
        summary["status"] = "blocked" if summary["blocking_count"] else "preview"
        if summary["matching_count"] == 0:
            summary["status"] = "empty"
        summary["deleted_count"] = 0
        summary["deleted_descendant_count"] = 0
        return summary

    def clear_pipeline_execution_history(self, pipeline_name: str) -> dict[str, Any]:
        """Delete terminal matching executions and their terminal descendants atomically."""
        name = pipeline_name.strip()
        if not name:
            raise ValueError("pipeline_name must not be empty")
        project_id = self._history_project_id()
        params = (project_id, name, project_id, project_id)

        with self.db.transaction() as conn:
            # Prevent new executions from appearing between the safety check
            # and the cascading delete.
            project_row = conn.execute(
                "SELECT id FROM projects WHERE id = %s FOR UPDATE",
                (project_id,),
            ).fetchone()
            if project_row is None:
                raise ValueError(f"Project {project_id} not found")
            rows = conn.execute(self._history_query(for_update=True), params).fetchall()
            summary = _summarize_history(rows, name, project_id)
            if summary["blocking_count"]:
                summary.update(
                    status="blocked",
                    deleted_count=0,
                    deleted_descendant_count=0,
                )
                return summary
            if summary["matching_count"] == 0:
                summary.update(
                    status="empty",
                    deleted_count=0,
                    deleted_descendant_count=0,
                )
                return summary

            deleted = conn.execute(
                """
                DELETE FROM pipeline_executions
                WHERE project_id = %s
                  AND pipeline_name = %s
                  AND status IN (%s, %s, %s)
                RETURNING id
                """,
                (
                    project_id,
                    name,
                    ExecutionStatus.COMPLETED.value,
                    ExecutionStatus.FAILED.value,
                    ExecutionStatus.CANCELLED.value,
                ),
            ).fetchall()

        deleted_count = len(deleted)
        summary.update(
            status="cleared",
            deleted_count=deleted_count,
            deleted_descendant_count=summary["selected_count"] - deleted_count,
            can_clear=False,
        )
        return summary
