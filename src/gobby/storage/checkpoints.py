"""Checkpoint storage manager.

Tracks shadow git checkpoints created when doom-looping agents are terminated,
preserving their uncommitted work as hidden git refs.
"""

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    id: str
    task_id: str
    session_id: str | None
    run_id: str
    ref_name: str
    commit_sha: str
    parent_sha: str
    files_changed: int
    message: str
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Checkpoint":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            run_id=row["run_id"],
            ref_name=row["ref_name"],
            commit_sha=row["commit_sha"],
            parent_sha=row["parent_sha"],
            files_changed=row["files_changed"],
            message=row["message"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LocalCheckpointManager:
    """CRUD operations for shadow git checkpoints."""

    def __init__(self, db: DatabaseProtocol) -> None:
        self.db = db

    def create(self, checkpoint: Checkpoint) -> Checkpoint:
        """Insert a new checkpoint record.

        Raises:
            sqlite3.IntegrityError: If a checkpoint with the same ID already exists.
        """
        self.db.execute(
            """INSERT INTO checkpoints
               (id, task_id, session_id, run_id, ref_name, commit_sha,
                parent_sha, files_changed, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                checkpoint.id,
                checkpoint.task_id,
                checkpoint.session_id,
                checkpoint.run_id,
                checkpoint.ref_name,
                checkpoint.commit_sha,
                checkpoint.parent_sha,
                checkpoint.files_changed,
                checkpoint.message,
                checkpoint.created_at,
            ),
        )
        return checkpoint

    def get(self, checkpoint_id: str) -> Checkpoint | None:
        """Get a checkpoint by ID."""
        row = self.db.fetchone("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,))
        return Checkpoint.from_row(row) if row else None

    def list_for_task(self, task_id: str) -> list[Checkpoint]:
        """List all checkpoints for a task, newest first."""
        rows = self.db.fetchall(
            "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,),
        )
        return [Checkpoint.from_row(row) for row in rows]

    def delete(self, checkpoint_id: str) -> bool:
        """Delete a checkpoint by ID. Returns True if deleted."""
        cursor = self.db.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
        return cursor.rowcount > 0

    def delete_old(self, task_id: str, keep_latest: int = 3) -> int:
        """Delete old checkpoints for a task, keeping N most recent.

        Returns number of checkpoints deleted.

        Raises:
            ValueError: If keep_latest < 1.
        """
        if keep_latest < 1:
            raise ValueError(f"keep_latest must be >= 1, got {keep_latest}")
        cursor = self.db.execute(
            """DELETE FROM checkpoints
               WHERE task_id = ? AND id NOT IN (
                   SELECT id FROM checkpoints
                   WHERE task_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?
               )""",
            (task_id, task_id, keep_latest),
        )
        return cursor.rowcount

    def count_for_task(self, task_id: str) -> int:
        """Count checkpoints for a task."""
        row = self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM checkpoints WHERE task_id = ?",
            (task_id,),
        )
        return row["cnt"] if row else 0
