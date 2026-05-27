"""Task compaction logic."""

from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.storage.tasks import LocalTaskManager


class TaskCompactor:
    """Handles compaction of old closed tasks."""

    def __init__(self, task_manager: LocalTaskManager) -> None:
        self.task_manager = task_manager

    def find_candidates(self, days_closed: int = 30) -> list[dict[str, Any]]:
        """
        Find tasks that have been closed for longer than the specified days
        and haven't been compacted yet.
        """
        cutoff = datetime.now(UTC) - timedelta(days=days_closed)
        cutoff_str = cutoff.isoformat()

        # Query directly since we need custom filtering not exposed by list_tasks
        sql = """
            SELECT * FROM tasks
            WHERE closed_at IS NOT NULL
              AND updated_at < %s
              AND compacted_at IS NULL
            ORDER BY updated_at ASC
        """
        rows = self.task_manager.db.fetchall(sql, (cutoff_str,))
        return [dict(row) for row in rows]

    def compact_task(self, task_id: str, summary: str) -> None:
        """
        Compact a task by replacing its description with a summary.
        """
        # Update database directly to set compacted_at
        now = datetime.now(UTC).isoformat()

        # We preserve the title but replace description with summary
        # and mark it as compacted.
        sql = """
            UPDATE tasks
            SET description = %s,
                compacted_at = %s,
                updated_at = %s
            WHERE id = %s
        """

        self.task_manager.db.execute(sql, (summary, now, now, task_id))
        self.task_manager._notify_listeners()

    def get_stats(self) -> dict[str, Any]:
        """Get compaction statistics."""
        sql_total = "SELECT COUNT(*) as c FROM tasks WHERE closed_at IS NOT NULL"
        sql_compacted = "SELECT COUNT(*) as c FROM tasks WHERE compacted_at IS NOT NULL"

        total = (self.task_manager.db.fetchone(sql_total) or {"c": 0})["c"]
        compacted = (self.task_manager.db.fetchone(sql_compacted) or {"c": 0})["c"]

        return {
            "total_closed": total,
            "compacted": compacted,
            "rate": round(compacted / total * 100, 1) if total > 0 else 0,
        }
