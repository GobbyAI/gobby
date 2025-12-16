import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from gobby.storage.database import LocalDatabase

logger = logging.getLogger(__name__)


@dataclass
class Task:
    id: str
    project_id: str
    title: str
    status: Literal["open", "in_progress", "closed"]
    priority: int
    type: str  # bug, feature, task, epic, chore
    created_at: str
    updated_at: str
    # Optional fields
    description: str | None = None
    parent_task_id: str | None = None
    discovered_in_session_id: str | None = None
    assignee: str | None = None
    labels: list[str] | None = None
    closed_reason: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        """Convert database row to Task object."""
        labels_json = row["labels"]
        labels = json.loads(labels_json) if labels_json else []

        return cls(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            status=row["status"],
            priority=row["priority"],
            type=row["type"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            description=row["description"],
            parent_task_id=row["parent_task_id"],
            discovered_in_session_id=row["discovered_in_session_id"],
            assignee=row["assignee"],
            labels=labels,
            closed_reason=row["closed_reason"],
        )


class TaskIDCollisionError(Exception):
    """Raised when a unique task ID cannot be generated."""

    pass


def generate_task_id(project_id: str, salt: str = "") -> str:
    """
    Generate a hash-based task ID.
    Format: gt-{hash} where hash is 6 hex chars.
    """
    # Use high-precision timestamp and random bytes
    # project_id is included to reduce collisions across projects
    data = f"{time.time_ns()}{os.urandom(8).hex()}{project_id}{salt}"
    hash_hex = hashlib.sha256(data.encode()).hexdigest()[:6]
    return f"gt-{hash_hex}"


# ...


class LocalTaskManager:
    def __init__(self, db: LocalDatabase):
        self.db = db
        self._change_listeners: list[Callable[[], Any]] = []

    def add_change_listener(self, listener: Callable[[], Any]) -> None:
        """Add a listener to be called when tasks change."""
        self._change_listeners.append(listener)

    def _notify_listeners(self) -> None:
        """Notify all listeners of a change."""
        for listener in self._change_listeners:
            try:
                listener()
            except Exception as e:
                logger.error(f"Error in task change listener: {e}")

    def create_task(
        self,
        project_id: str,
        title: str,
        description: str | None = None,
        parent_task_id: str | None = None,
        discovered_in_session_id: str | None = None,
        priority: int = 2,
        type: str = "task",
        assignee: str | None = None,
        labels: list[str] | None = None,
    ) -> Task:
        """Create a new task with collision handling."""
        max_retries = 3
        now = datetime.now(timezone.utc).isoformat()

        # Serialize labels
        labels_json = json.dumps(labels) if labels else None

        for attempt in range(max_retries + 1):
            try:
                task_id = generate_task_id(project_id, salt=str(attempt))

                with self.db.transaction() as conn:
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            id, project_id, title, description, parent_task_id,
                            discovered_in_session_id, priority, type, assignee,
                            labels, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                        """,
                        (
                            task_id,
                            project_id,
                            title,
                            description,
                            parent_task_id,
                            discovered_in_session_id,
                            priority,
                            type,
                            assignee,
                            labels_json,
                            now,
                            now,
                        ),
                    )

                logger.debug(f"Created task {task_id} in project {project_id}")
                self._notify_listeners()
                return self.get_task(task_id)

            except sqlite3.IntegrityError as e:
                # Check if it's a primary key violation (ID collision)
                if "UNIQUE constraint failed: tasks.id" in str(e) or "tasks.id" in str(e):
                    if attempt == max_retries:
                        raise TaskIDCollisionError(
                            f"Failed to generate unique task ID after {max_retries} retries"
                        ) from e
                    logger.warning(f"Task ID collision for {task_id}, retrying...")
                    continue
                raise e

        raise TaskIDCollisionError("Unreachable")

    def get_task(self, task_id: str) -> Task:
        """Get a task by ID."""
        row = self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not row:
            raise ValueError(f"Task {task_id} not found")
        return Task.from_row(row)

    def update_task(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: int | None = None,
        type: str | None = None,
        assignee: str | None = None,
        labels: list[str] | None = None,
        parent_task_id: str | None = None,
    ) -> Task:
        """Update task fields."""
        updates = []
        params: list[Any] = []

        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if priority is not None:
            updates.append("priority = ?")
            params.append(priority)
        if type is not None:
            updates.append("type = ?")
            params.append(type)
        if assignee is not None:
            updates.append("assignee = ?")
            params.append(assignee)
        if labels is not None:
            updates.append("labels = ?")
            params.append(json.dumps(labels))
        if parent_task_id is not None:
            updates.append("parent_task_id = ?")
            # Note: explicit None means clear parent
            params.append(parent_task_id)

        if not updates:
            return self.get_task(task_id)

        updates.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())

        params.append(task_id)  # for WHERE clause

        sql = f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?"

        with self.db.transaction() as conn:
            cursor = conn.execute(sql, tuple(params))
            if cursor.rowcount == 0:
                raise ValueError(f"Task {task_id} not found")

        self._notify_listeners()
        return self.get_task(task_id)

    def close_task(self, task_id: str, reason: str | None = None) -> Task:
        """Close a task."""
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = 'closed', closed_reason = ?, updated_at = ? WHERE id = ?",
                (reason, now, task_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Task {task_id} not found")

        self._notify_listeners()
        return self.get_task(task_id)

    def delete_task(self, task_id: str, cascade: bool = False) -> None:
        """Delete a task. If cascade is True, delete children recursively."""
        if not cascade:
            # Check for children
            row = self.db.fetchone("SELECT 1 FROM tasks WHERE parent_task_id = ?", (task_id,))
            if row:
                raise ValueError(f"Task {task_id} has children. Use cascade=True to delete.")

        if cascade:
            # Recursive delete
            # Find all children
            children = self.db.fetchall("SELECT id FROM tasks WHERE parent_task_id = ?", (task_id,))
            for child in children:
                self.delete_task(child["id"], cascade=True)

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._notify_listeners()

    def list_tasks(
        self,
        project_id: str | None = None,
        status: str | None = None,
        priority: int | None = None,
        assignee: str | None = None,
        parent_task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """List tasks with filtering."""
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []

        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if priority:
            query += " AND priority = ?"
            params.append(priority)
        if assignee:
            query += " AND assignee = ?"
            params.append(assignee)
        if parent_task_id:
            query += " AND parent_task_id = ?"
            params.append(parent_task_id)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.db.fetchall(query, tuple(params))
        return [Task.from_row(row) for row in rows]

    def list_ready_tasks(
        self,
        project_id: str | None = None,
        priority: int | None = None,
        assignee: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """List tasks that are open and not blocked by any open blocking dependency."""
        query = """
        SELECT t.* FROM tasks t
        WHERE t.status = 'open'
        AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on = blocker.id
            WHERE d.task_id = t.id
              AND d.dep_type = 'blocks'
              AND blocker.status != 'closed'
        )
        """
        params: list[Any] = []

        if project_id:
            query += " AND t.project_id = ?"
            params.append(project_id)
        if priority:
            query += " AND t.priority = ?"
            params.append(priority)
        if assignee:
            query += " AND t.assignee = ?"
            params.append(assignee)

        query += " ORDER BY t.priority ASC, t.created_at ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.db.fetchall(query, tuple(params))
        return [Task.from_row(row) for row in rows]

    def list_blocked_tasks(
        self,
        project_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """List tasks that are blocked by at least one open blocking dependency."""
        query = """
        SELECT t.* FROM tasks t
        WHERE t.status = 'open'
        AND EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on = blocker.id
            WHERE d.task_id = t.id
              AND d.dep_type = 'blocks'
              AND blocker.status != 'closed'
        )
        """
        params: list[Any] = []

        if project_id:
            query += " AND t.project_id = ?"
            params.append(project_id)

        query += " ORDER BY t.priority ASC, t.created_at ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.db.fetchall(query, tuple(params))
        return [Task.from_row(row) for row in rows]
