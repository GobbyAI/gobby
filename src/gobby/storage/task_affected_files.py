"""Task affected files storage manager.

Tracks which files each task is expected to touch, enabling file-based
dependency analysis and contention detection between concurrent tasks.
"""

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

AnnotationSource = Literal["expansion", "manual", "observed"]


@dataclass
class TaskAffectedFile:
    id: int
    task_id: str
    file_path: str
    annotation_source: AnnotationSource
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskAffectedFile":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            file_path=row["file_path"],
            annotation_source=row["annotation_source"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "file_path": self.file_path,
            "annotation_source": self.annotation_source,
            "created_at": self.created_at,
        }


class TaskAffectedFileManager:
    def __init__(self, db: DatabaseProtocol):
        self.db = db

    def set_files(
        self,
        task_id: str,
        files: list[str],
        source: AnnotationSource = "expansion",
    ) -> list[TaskAffectedFile]:
        """Bulk set affected files for a task.

        Replaces all existing files for the given source with the new list.
        Files from other sources are preserved.
        """
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM task_affected_files WHERE task_id = ? AND annotation_source = ?",
                (task_id, source),
            )
            results = []
            for file_path in files:
                try:
                    cursor = conn.execute(
                        "INSERT INTO task_affected_files (task_id, file_path, annotation_source) "
                        "VALUES (?, ?, ?)",
                        (task_id, file_path, source),
                    )
                    row = conn.execute(
                        "SELECT * FROM task_affected_files WHERE id = ?",
                        (cursor.lastrowid,),
                    ).fetchone()
                    if row:
                        results.append(TaskAffectedFile.from_row(row))
                except sqlite3.IntegrityError:
                    # UNIQUE constraint — file already exists from another source
                    logger.debug(f"File {file_path} already tracked for task {task_id}")
            return results

    def get_files(self, task_id: str) -> list[TaskAffectedFile]:
        """Get all affected files for a task."""
        rows = self.db.fetchall(
            "SELECT * FROM task_affected_files WHERE task_id = ? ORDER BY file_path",
            (task_id,),
        )
        return [TaskAffectedFile.from_row(row) for row in rows]

    def add_file(
        self,
        task_id: str,
        file_path: str,
        source: AnnotationSource = "manual",
    ) -> TaskAffectedFile | None:
        """Add a single affected file to a task.

        Returns None if the file is already tracked.
        """
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO task_affected_files (task_id, file_path, annotation_source) "
                    "VALUES (?, ?, ?)",
                    (task_id, file_path, source),
                )
                row = conn.execute(
                    "SELECT * FROM task_affected_files WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                return TaskAffectedFile.from_row(row) if row else None
        except sqlite3.IntegrityError:
            logger.debug(f"File {file_path} already tracked for task {task_id}")
            return None

    def remove_file(self, task_id: str, file_path: str) -> bool:
        """Remove a single affected file from a task."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM task_affected_files WHERE task_id = ? AND file_path = ?",
                (task_id, file_path),
            )
            deleted: bool = cursor.rowcount > 0
            return deleted

    def find_overlapping_tasks(self, task_ids: list[str]) -> dict[tuple[str, str], list[str]]:
        """Find tasks that share affected files.

        Args:
            task_ids: List of task IDs to check for overlaps.

        Returns:
            Dict mapping (task_a, task_b) pairs to lists of shared file paths.
            Only pairs with at least one shared file are included.
            Pairs are ordered so task_a < task_b lexicographically.
        """
        if len(task_ids) < 2:
            return {}

        placeholders = ",".join("?" * len(task_ids))
        rows = self.db.fetchall(
            f"SELECT task_id, file_path FROM task_affected_files "
            f"WHERE task_id IN ({placeholders}) ORDER BY file_path",
            tuple(task_ids),
        )

        # Build file -> tasks index
        file_tasks: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            file_tasks[row["file_path"]].append(row["task_id"])

        # Find overlapping pairs
        overlaps: dict[tuple[str, str], list[str]] = defaultdict(list)
        for file_path, tasks in file_tasks.items():
            if len(tasks) < 2:
                continue
            unique_tasks = sorted(set(tasks))
            for i in range(len(unique_tasks)):
                for j in range(i + 1, len(unique_tasks)):
                    pair = (unique_tasks[i], unique_tasks[j])
                    overlaps[pair].append(file_path)

        return dict(overlaps)

    def get_tasks_for_file(self, file_path: str) -> list[TaskAffectedFile]:
        """Reverse lookup: get all tasks that affect a given file."""
        rows = self.db.fetchall(
            "SELECT * FROM task_affected_files WHERE file_path = ? ORDER BY task_id",
            (file_path,),
        )
        return [TaskAffectedFile.from_row(row) for row in rows]
