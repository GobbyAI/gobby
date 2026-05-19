"""Local clone storage manager.

Manages local git clones for parallel development, distinct from worktrees.
Clones are full repository copies while worktrees share a single .git directory.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from gobby.storage.database import DatabaseProtocol
from gobby.utils.id import generate_prefixed_id

logger = logging.getLogger(__name__)


class CloneStatus(str, Enum):
    """Clone status values."""

    ACTIVE = "active"
    SYNCING = "syncing"
    STALE = "stale"
    MERGED = "merged"
    CLEANUP = "cleanup"


@dataclass
class Clone:
    """Clone data model."""

    id: str
    project_id: str
    branch_name: str
    clone_path: str
    base_branch: str
    task_id: str | None
    agent_session_id: str | None
    status: str
    remote_url: str | None
    last_sync_at: str | None
    cleanup_after: str | None
    created_at: str
    updated_at: str
    workspace_role: str = "task"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Clone:
        """Create Clone from database row."""

        def _safe_get(field: str) -> Any:
            if hasattr(row, "get"):
                return row.get(field)
            try:
                return row[field]
            except (KeyError, IndexError, TypeError):
                return None

        return cls(
            id=row["id"],
            project_id=row["project_id"],
            branch_name=row["branch_name"],
            clone_path=row["clone_path"],
            base_branch=row["base_branch"],
            task_id=row["task_id"],
            agent_session_id=row["agent_session_id"],
            status=row["status"],
            remote_url=row["remote_url"],
            last_sync_at=row["last_sync_at"],
            cleanup_after=row["cleanup_after"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            workspace_role=_safe_get("workspace_role") or "task",
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "branch_name": self.branch_name,
            "clone_path": self.clone_path,
            "base_branch": self.base_branch,
            "task_id": self.task_id,
            "agent_session_id": self.agent_session_id,
            "status": self.status,
            "remote_url": self.remote_url,
            "last_sync_at": self.last_sync_at,
            "cleanup_after": self.cleanup_after,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workspace_role": self.workspace_role,
        }

    def to_brief(self) -> dict[str, Any]:
        """Slim representation for list operations."""
        return {
            "id": self.id,
            "branch_name": self.branch_name,
            "clone_path": self.clone_path,
            "status": self.status,
            "task_id": self.task_id,
            "agent_session_id": self.agent_session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workspace_role": self.workspace_role,
        }


class LocalCloneManager:
    """Manager for local clone storage."""

    def __init__(self, db: DatabaseProtocol):
        """Initialize with database connection."""
        self.db = db

    def create(
        self,
        project_id: str,
        branch_name: str,
        clone_path: str,
        base_branch: str = "main",
        task_id: str | None = None,
        agent_session_id: str | None = None,
        remote_url: str | None = None,
        cleanup_after: str | None = None,
        workspace_role: str = "task",
    ) -> Clone:
        """
        Create a new clone record.

        Args:
            project_id: Project ID
            branch_name: Git branch name
            clone_path: Absolute path to clone directory
            base_branch: Base branch for the clone
            task_id: Optional task ID to link
            agent_session_id: Optional session ID that owns this clone
            remote_url: Optional remote URL of the repository
            cleanup_after: Optional ISO timestamp for automatic cleanup

        Returns:
            Created Clone instance
        """
        clone_id = generate_prefixed_id("clone", length=6)
        now = datetime.now(UTC).isoformat()

        self.db.execute(
            """
            INSERT INTO clones (
                id, project_id, branch_name, clone_path, base_branch,
                task_id, agent_session_id, status, remote_url,
                last_sync_at, cleanup_after, created_at, updated_at,
                workspace_role
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clone_id,
                project_id,
                branch_name,
                clone_path,
                base_branch,
                task_id,
                agent_session_id,
                CloneStatus.ACTIVE.value,
                remote_url,
                None,  # last_sync_at
                cleanup_after,
                now,
                now,
                workspace_role,
            ),
        )

        return Clone(
            id=clone_id,
            project_id=project_id,
            branch_name=branch_name,
            clone_path=clone_path,
            base_branch=base_branch,
            task_id=task_id,
            agent_session_id=agent_session_id,
            status=CloneStatus.ACTIVE.value,
            remote_url=remote_url,
            last_sync_at=None,
            cleanup_after=cleanup_after,
            created_at=now,
            updated_at=now,
            workspace_role=workspace_role,
        )

    def get(self, clone_id: str) -> Clone | None:
        """Get clone by ID."""
        row = self.db.fetchone("SELECT * FROM clones WHERE id = ?", (clone_id,))
        return Clone.from_row(row) if row else None

    def get_by_task(self, task_id: str) -> Clone | None:
        """Get clone linked to a task."""
        row = self.db.fetchone("SELECT * FROM clones WHERE task_id = ?", (task_id,))
        return Clone.from_row(row) if row else None

    def get_by_path(self, clone_path: str) -> Clone | None:
        """Get clone by path."""
        row = self.db.fetchone("SELECT * FROM clones WHERE clone_path = ?", (clone_path,))
        return Clone.from_row(row) if row else None

    def get_by_branch(self, project_id: str, branch_name: str) -> Clone | None:
        """Get clone by project and branch name."""
        row = self.db.fetchone(
            "SELECT * FROM clones WHERE project_id = ? AND branch_name = ?",
            (project_id, branch_name),
        )
        return Clone.from_row(row) if row else None

    def list_clones(
        self,
        project_id: str | None = None,
        status: str | None = None,
        agent_session_id: str | None = None,
        limit: int = 50,
    ) -> list[Clone]:
        """
        List clones with optional filters.

        Args:
            project_id: Filter by project
            status: Filter by status
            agent_session_id: Filter by owning session
            limit: Maximum number of results

        Returns:
            List of Clone instances
        """
        conditions = []
        params: list[Any] = []

        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if agent_session_id:
            conditions.append("agent_session_id = ?")
            params.append(agent_session_id)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT * FROM clones
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
            """,  # nosec B608
            tuple(params),
        )
        return [Clone.from_row(row) for row in rows]

    # Allowlist of valid clone column names to prevent SQL injection
    _VALID_UPDATE_FIELDS = frozenset(
        {
            "branch_name",
            "base_branch",
            "clone_path",
            "status",
            "agent_session_id",
            "task_id",
            "remote_url",
            "last_sync_at",
            "cleanup_after",
            "workspace_role",
            "updated_at",
        }
    )

    def update(self, clone_id: str, **fields: Any) -> Clone | None:
        """
        Update clone fields.

        Args:
            clone_id: Clone ID to update
            **fields: Fields to update (must be valid column names)

        Returns:
            Updated Clone or None if not found

        Raises:
            ValueError: If any field name is not in the allowlist
        """
        if not fields:
            return self.get(clone_id)

        # Validate field names against allowlist to prevent SQL injection
        invalid_fields = set(fields.keys()) - self._VALID_UPDATE_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid field names: {invalid_fields}")

        # Add updated_at timestamp
        fields["updated_at"] = datetime.now(UTC).isoformat()

        set_clause = ", ".join(f"{key} = ?" for key in fields.keys())
        values = list(fields.values()) + [clone_id]

        self.db.execute(
            f"UPDATE clones SET {set_clause} WHERE id = ?",  # nosec B608
            tuple(values),
        )

        return self.get(clone_id)

    def delete(self, clone_id: str) -> bool:
        """
        Delete clone record.

        Args:
            clone_id: Clone ID to delete

        Returns:
            True if deleted, False if not found
        """
        cursor = self.db.execute("DELETE FROM clones WHERE id = ?", (clone_id,))
        return cursor.rowcount > 0

    # Status transition methods

    def mark_syncing(self, clone_id: str) -> Clone | None:
        """
        Mark clone as syncing.

        Args:
            clone_id: Clone ID

        Returns:
            Updated Clone or None if not found
        """
        return self.update(clone_id, status=CloneStatus.SYNCING.value)

    def mark_stale(self, clone_id: str) -> Clone | None:
        """
        Mark clone as stale (inactive).

        Args:
            clone_id: Clone ID

        Returns:
            Updated Clone or None if not found
        """
        return self.update(clone_id, status=CloneStatus.STALE.value)

    def mark_cleanup(self, clone_id: str) -> Clone | None:
        """
        Mark clone for cleanup.

        Args:
            clone_id: Clone ID

        Returns:
            Updated Clone or None if not found
        """
        return self.update(clone_id, status=CloneStatus.CLEANUP.value)

    def mark_merged(self, clone_id: str, cleanup_after: str | None = None) -> Clone | None:
        """Mark clone as merged and optionally schedule cleanup."""
        return self.update(
            clone_id,
            status=CloneStatus.MERGED.value,
            cleanup_after=cleanup_after,
        )

    def record_sync(self, clone_id: str) -> Clone | None:
        """
        Record a sync operation on a clone.

        Args:
            clone_id: Clone ID

        Returns:
            Updated Clone or None if not found
        """
        now = datetime.now(UTC).isoformat()
        return self.update(
            clone_id,
            status=CloneStatus.ACTIVE.value,
            last_sync_at=now,
        )

    def claim(self, clone_id: str, session_id: str) -> Clone | None:
        """
        Claim a clone for an agent session.

        Args:
            clone_id: Clone ID
            session_id: Session ID claiming ownership

        Returns:
            Updated Clone or None if not found
        """
        return self.update(clone_id, agent_session_id=session_id)

    def release(self, clone_id: str) -> Clone | None:
        """
        Release a clone from its current owner.

        Args:
            clone_id: Clone ID

        Returns:
            Updated Clone or None if not found
        """
        return self.update(clone_id, agent_session_id=None)

    def count_by_status(self, project_id: str) -> dict[str, int]:
        """
        Get count of clones by status for a project.

        Args:
            project_id: Project ID

        Returns:
            Dict mapping status to count
        """
        rows = self.db.fetchall(
            """
            SELECT status, COUNT(*) as count
            FROM clones
            WHERE project_id = ?
            GROUP BY status
            """,
            (project_id,),
        )
        return {row["status"]: row["count"] for row in rows}

    def find_stale(
        self,
        project_id: str,
        hours: int = 24,
        limit: int = 50,
    ) -> list[Clone]:
        """
        Find clones that are stale (no activity for N hours).

        Finds active clones with no agent_session_id and updated_at
        older than the threshold.

        Args:
            project_id: Project ID
            hours: Hours of inactivity threshold
            limit: Maximum number of results

        Returns:
            List of stale Clone instances
        """
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

        rows = self.db.fetchall(
            """
            SELECT * FROM clones
            WHERE project_id = ?
              AND status = ?
              AND agent_session_id IS NULL
              AND updated_at < ?
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (project_id, CloneStatus.ACTIVE.value, cutoff, limit),
        )
        return [Clone.from_row(row) for row in rows]

    def find_expired(
        self,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[Clone]:
        """
        Find clones past their cleanup window.

        These are clones where merge succeeded and the cleanup_after
        grace period has elapsed. Safe to delete — work is in target branch.

        Args:
            project_id: Optional project filter (None = all projects)
            limit: Maximum number of results

        Returns:
            List of expired Clone instances
        """
        now = datetime.now(UTC).isoformat()
        if project_id:
            rows = self.db.fetchall(
                """
                SELECT * FROM clones
                WHERE project_id = ?
                  AND cleanup_after IS NOT NULL
                  AND cleanup_after < ?
                ORDER BY cleanup_after ASC
                LIMIT ?
                """,
                (project_id, now, limit),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM clones
                WHERE cleanup_after IS NOT NULL
                  AND cleanup_after < ?
                ORDER BY cleanup_after ASC
                LIMIT ?
                """,
                (now, limit),
            )
        return [Clone.from_row(row) for row in rows]

    def cleanup_stale(
        self,
        project_id: str,
        hours: int = 24,
        dry_run: bool = True,
    ) -> list[Clone]:
        """
        Mark stale clones for cleanup.

        Args:
            project_id: Project ID
            hours: Hours of inactivity threshold
            dry_run: If True, just return candidates without updating

        Returns:
            List of clones marked/to be marked as stale.
            When dry_run is False, returns refreshed clones with updated status.
        """
        stale = self.find_stale(project_id, hours)

        if not dry_run:
            updated: list[Clone] = []
            for clone in stale:
                result = self.mark_stale(clone.id)
                if result is not None:
                    updated.append(result)
            return updated

        return stale
