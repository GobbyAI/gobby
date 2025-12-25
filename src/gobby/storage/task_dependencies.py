import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.database import LocalDatabase

logger = logging.getLogger(__name__)

DependencyType = Literal["blocks", "related", "discovered-from"]


@dataclass
class TaskDependency:
    id: int
    task_id: str
    depends_on: str
    dep_type: DependencyType
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskDependency":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            depends_on=row["depends_on"],
            dep_type=row["dep_type"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert TaskDependency to dictionary."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "depends_on": self.depends_on,
            "dep_type": self.dep_type,
            "created_at": self.created_at,
        }


class DependencyCycleError(Exception):
    """Raised when a dependency cycle is detected."""

    pass


class TaskDependencyManager:
    def __init__(self, db: LocalDatabase):
        self.db = db

    def add_dependency(
        self, task_id: str, depends_on: str, dep_type: DependencyType = "blocks"
    ) -> TaskDependency:
        """Add a dependency."""
        if task_id == depends_on:
            raise ValueError("Task cannot depend on itself")

        # For 'blocks', prevent cycles
        if dep_type == "blocks" and self._would_create_cycle(task_id, depends_on):
            raise DependencyCycleError(
                f"Adding dependency {task_id} blocks {depends_on} would create a cycle"
            )

        now = datetime.now(UTC).isoformat()

        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, ?, ?)",
                (task_id, depends_on, dep_type, now),
            )
            dep_id = cursor.lastrowid

            if dep_id is None:
                raise ValueError("Failed to retrieve dependency ID")

            return TaskDependency(dep_id, task_id, depends_on, dep_type, now)

    def remove_dependency(self, task_id: str, depends_on: str) -> bool:
        """Remove a dependency."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on = ?",
                (task_id, depends_on),
            )
            deleted: bool = cursor.rowcount > 0
            return deleted

    def get_blockers(self, task_id: str) -> list[TaskDependency]:
        """Get tasks that block this task (task_id depends on X)."""
        rows = self.db.fetchall(
            "SELECT * FROM task_dependencies WHERE task_id = ? AND dep_type = 'blocks'",
            (task_id,),
        )
        return [TaskDependency.from_row(row) for row in rows]

    def get_blocking(self, task_id: str) -> list[TaskDependency]:
        """Get tasks that this task blocks (X depends on task_id)."""
        rows = self.db.fetchall(
            "SELECT * FROM task_dependencies WHERE depends_on = ? AND dep_type = 'blocks'",
            (task_id,),
        )
        return [TaskDependency.from_row(row) for row in rows]

    def get_all_dependencies(self, task_id: str) -> list[TaskDependency]:
        """Get all dependencies for a task (outgoing edges)."""
        rows = self.db.fetchall(
            "SELECT * FROM task_dependencies WHERE task_id = ?",
            (task_id,),
        )
        return [TaskDependency.from_row(row) for row in rows]

    def _would_create_cycle(self, task_id: str, depends_on: str) -> bool:
        """
        Check if adding edge task_id -> depends_on creates a cycle.
        This implies exists path depends_on -> ... -> task_id.
        """
        visited = set()
        stack = [depends_on]

        while stack:
            current = stack.pop()
            if current == task_id:
                return True

            if current in visited:
                continue
            visited.add(current)

            deps = self.db.fetchall(
                "SELECT depends_on FROM task_dependencies WHERE task_id = ? AND dep_type = 'blocks'",
                (current,),
            )
            for row in deps:
                stack.append(row["depends_on"])

        return False

    def get_dependency_tree(
        self,
        task_id: str,
        direction: Literal["blockers", "blocking", "both"] = "both",
        max_depth: int = 10,
    ) -> dict[str, Any]:
        """
        Get dependency tree.
        direction:
          - blockers: tasks that task_id depends on (upstream)
          - blocking: tasks that depend on task_id (downstream)
          - both: both
        """
        result: dict[str, Any] = {"id": task_id}

        if max_depth <= 0:
            result["_truncated"] = True
            return result

        if direction in ("blockers", "both"):
            blockers = self.get_blockers(task_id)
            if blockers:
                result["blockers"] = [
                    self.get_dependency_tree(
                        b.depends_on, direction="blockers", max_depth=max_depth - 1
                    )
                    for b in blockers
                ]

        if direction in ("blocking", "both"):
            blocking = self.get_blocking(task_id)
            if blocking:
                # blocking contains deps where depends_on = task_id. The other end is task_id.
                # Use b.task_id (the task that is blocked).
                result["blocking"] = [
                    self.get_dependency_tree(
                        b.task_id, direction="blocking", max_depth=max_depth - 1
                    )
                    for b in blocking
                ]

        return result

    def check_cycles(self) -> list[list[str]]:
        """Detect all cycles in 'blocks' dependencies. Returns list of cycles (list of task IDs)."""
        rows = self.db.fetchall(
            "SELECT task_id, depends_on FROM task_dependencies WHERE dep_type = 'blocks'"
        )
        graph: dict[str, list[str]] = {}
        for row in rows:
            u, v = row["task_id"], row["depends_on"]
            graph.setdefault(u, []).append(v)
            graph.setdefault(v, [])

        cycles = []
        visited = set()
        path = []
        path_set = set()

        def dfs(u: str) -> None:
            visited.add(u)
            path.append(u)
            path_set.add(u)

            for v in graph.get(u, []):
                if v in path_set:
                    # Cycle found
                    # cycle is from v to ... to u to v
                    try:
                        idx = path.index(v)
                        cycles.append(path[idx:].copy())
                    except ValueError:
                        pass
                elif v not in visited:
                    dfs(v)

            path_set.remove(u)
            path.pop()

        for node in list(graph.keys()):
            if node not in visited:
                dfs(node)

        return cycles
