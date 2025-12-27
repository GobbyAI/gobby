import hashlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.database import LocalDatabase

logger = logging.getLogger(__name__)


@dataclass
class Task:
    id: str
    project_id: str
    title: str
    status: Literal["open", "in_progress", "closed"]
    priority: int
    task_type: str  # bug, feature, task, epic, chore
    created_at: str
    updated_at: str
    # Optional fields
    description: str | None = None
    parent_task_id: str | None = None
    discovered_in_session_id: str | None = None
    assignee: str | None = None
    labels: list[str] | None = None
    closed_reason: str | None = None
    platform_id: str | None = None
    validation_status: Literal["pending", "valid", "invalid"] | None = None
    validation_feedback: str | None = None
    original_instruction: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        """Convert database row to Task object."""
        labels_json = row["labels"]
        labels = json.loads(labels_json) if labels_json else []

        # Handle optional columns that might not exist yet if migration pending
        keys = row.keys()

        return cls(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            status=row["status"],
            priority=row["priority"],
            task_type=row["type"],  # DB column is 'type'
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            description=row["description"],
            parent_task_id=row["parent_task_id"],
            discovered_in_session_id=row["discovered_in_session_id"],
            assignee=row["assignee"],
            labels=labels,
            closed_reason=row["closed_reason"],
            platform_id=row["platform_id"] if "platform_id" in keys else None,
            validation_status=row["validation_status"] if "validation_status" in keys else None,
            validation_feedback=row["validation_feedback"]
            if "validation_feedback" in keys
            else None,
            original_instruction=row["original_instruction"]
            if "original_instruction" in keys
            else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert Task to dictionary."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "type": self.task_type,  # Use 'type' for API compatibility
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "description": self.description,
            "parent_task_id": self.parent_task_id,
            "discovered_in_session_id": self.discovered_in_session_id,
            "assignee": self.assignee,
            "labels": self.labels,
            "closed_reason": self.closed_reason,
            "platform_id": self.platform_id,
            "validation_status": self.validation_status,
            "validation_feedback": self.validation_feedback,
            "original_instruction": self.original_instruction,
        }


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
        task_type: str = "task",
        assignee: str | None = None,
        labels: list[str] | None = None,
        original_instruction: str | None = None,
    ) -> Task:
        """Create a new task with collision handling."""
        max_retries = 3
        now = datetime.now(UTC).isoformat()

        # Serialize labels
        labels_json = json.dumps(labels) if labels else None
        task_id = ""

        # Default validation status
        validation_status = "pending" if original_instruction else None

        for attempt in range(max_retries + 1):
            try:
                task_id = generate_task_id(project_id, salt=str(attempt))

                with self.db.transaction() as conn:
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            id, project_id, title, description, parent_task_id,
                            discovered_in_session_id, priority, type, assignee,
                            labels, status, created_at, updated_at,
                            original_instruction, validation_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                        """,
                        (
                            task_id,
                            project_id,
                            title,
                            description,
                            parent_task_id,
                            discovered_in_session_id,
                            priority,
                            task_type,  # DB column is 'type'
                            assignee,
                            labels_json,
                            now,
                            now,
                            original_instruction,
                            validation_status,
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

    def find_task_by_prefix(self, prefix: str) -> Task | None:
        """Find a task by ID prefix. Returns None if no match or multiple matches."""
        # First try exact match
        row = self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (prefix,))
        if row:
            return Task.from_row(row)

        # Try prefix match
        rows = self.db.fetchall("SELECT * FROM tasks WHERE id LIKE ?", (f"{prefix}%",))
        if len(rows) == 1:
            return Task.from_row(rows[0])
        return None

    def find_tasks_by_prefix(self, prefix: str) -> list[Task]:
        """Find all tasks matching an ID prefix."""
        rows = self.db.fetchall("SELECT * FROM tasks WHERE id LIKE ?", (f"{prefix}%",))
        return [Task.from_row(row) for row in rows]

    def update_task(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: int | None = None,
        task_type: str | None = None,
        assignee: str | None = None,
        labels: list[str] | None = None,
        parent_task_id: str | None = None,
        validation_status: str | None = None,
        validation_feedback: str | None = None,
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
        if task_type is not None:
            updates.append("type = ?")  # DB column is 'type'
            params.append(task_type)
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
        if validation_status is not None:
            updates.append("validation_status = ?")
            params.append(validation_status)
        if validation_feedback is not None:
            updates.append("validation_feedback = ?")
            params.append(validation_feedback)

        if not updates:
            return self.get_task(task_id)

        updates.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())

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
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET status = 'closed', closed_reason = ?, updated_at = ? WHERE id = ?",
                (reason, now, task_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Task {task_id} not found")

        self._notify_listeners()
        return self.get_task(task_id)

    def add_label(self, task_id: str, label: str) -> Task:
        """Add a label to a task if not present."""
        task = self.get_task(task_id)
        labels = task.labels or []
        if label not in labels:
            labels.append(label)
            return self.update_task(task_id, labels=labels)
        return task

    def remove_label(self, task_id: str, label: str) -> Task:
        """Remove a label from a task if present."""
        task = self.get_task(task_id)
        labels = task.labels or []
        if label in labels:
            labels.remove(label)
            return self.update_task(task_id, labels=labels)
        return task

    def delete_task(self, task_id: str, cascade: bool = False) -> bool:
        """Delete a task. If cascade is True, delete children recursively.

        Returns:
            True if task was deleted, False if task not found.
        """
        # Check if task exists first
        existing = self.db.fetchone("SELECT 1 FROM tasks WHERE id = ?", (task_id,))
        if not existing:
            return False

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
        return True

    def list_tasks(
        self,
        project_id: str | None = None,
        status: str | None = None,
        priority: int | None = None,
        assignee: str | None = None,
        task_type: str | None = None,
        label: str | None = None,
        parent_task_id: str | None = None,
        title_like: str | None = None,
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
        if task_type:
            query += " AND type = ?"  # DB column is 'type'
            params.append(task_type)
        if label:
            # tasks.labels is a JSON list. We use json_each to find if the label is in the list.
            # Example: WHERE EXISTS (SELECT 1 FROM json_each(tasks.labels) WHERE value = ?)
            query += " AND EXISTS (SELECT 1 FROM json_each(tasks.labels) WHERE value = ?)"
            params.append(label)
        if parent_task_id:
            query += " AND parent_task_id = ?"
            params.append(parent_task_id)
        if title_like:
            query += " AND title LIKE ?"
            params.append(f"%{title_like}%")

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.db.fetchall(query, tuple(params))
        return [Task.from_row(row) for row in rows]

    def list_ready_tasks(
        self,
        project_id: str | None = None,
        priority: int | None = None,
        task_type: str | None = None,
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
        if task_type:
            query += " AND t.type = ?"  # DB column is 'type'
            params.append(task_type)
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
