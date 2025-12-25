"""Validation utilities for task system."""

import logging
from typing import Any

from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


class TaskValidator:
    """Validates task data integrity."""

    def __init__(self, task_manager: LocalTaskManager) -> None:
        self.task_manager = task_manager
        self.db = task_manager.db

    def check_orphan_dependencies(self) -> list[dict[str, Any]]:
        """
        Check for dependencies where one of the tasks does not exist.
        """
        sql = """
            SELECT d.id, d.task_id, d.depends_on, d.dep_type
            FROM task_dependencies d
            LEFT JOIN tasks t1 ON d.task_id = t1.id
            LEFT JOIN tasks t2 ON d.depends_on = t2.id
            WHERE t1.id IS NULL OR t2.id IS NULL
        """
        rows = self.db.fetchall(sql)
        return [dict(row) for row in rows]

    def check_invalid_projects(self) -> list[dict[str, Any]]:
        """
        Check for tasks linked to non-existent projects.
        """
        sql = """
            SELECT t.id, t.title, t.project_id
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE p.id IS NULL
        """
        rows = self.db.fetchall(sql)
        return [dict(row) for row in rows]

    def check_cycles(self) -> list[list[str]]:
        """
        Check for dependency cycles.
        Wrapper around TaskDependencyManager.check_cycles but accessible here.
        """
        from gobby.storage.task_dependencies import TaskDependencyManager

        dep_manager = TaskDependencyManager(self.db)
        cycles: list[list[str]] = dep_manager.check_cycles()
        return cycles

    def clean_orphans(self) -> int:
        """
        Remove orphaned dependencies.
        Returns count of removed rows.
        """
        orphans = self.check_orphan_dependencies()
        if not orphans:
            return 0

        ids = [str(row["id"]) for row in orphans]
        placeholders = ",".join("?" for _ in ids)

        with self.db.transaction() as conn:
            conn.execute(f"DELETE FROM task_dependencies WHERE id IN ({placeholders})", tuple(ids))

        return len(ids)

    def validate_all(self) -> dict[str, Any]:
        """Run all validation checks."""
        return {
            "orphan_dependencies": self.check_orphan_dependencies(),
            "invalid_projects": self.check_invalid_projects(),
            "cycles": self.check_cycles(),
        }
