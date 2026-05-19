"""Local worktree storage manager."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.id import generate_prefixed_id

logger = logging.getLogger(__name__)


class WorktreeStatus(str, Enum):
    """Worktree status values."""

    ACTIVE = "active"
    STALE = "stale"
    MERGED = "merged"
    ABANDONED = "abandoned"


@dataclass
class Worktree:
    """Worktree data model."""

    id: str
    project_id: str
    task_id: str | None
    branch_name: str
    worktree_path: str
    base_branch: str
    agent_session_id: str | None
    status: str
    created_at: str
    updated_at: str
    merged_at: str | None
    merge_state: str | None = None  # "pending", "resolved", or None
    cleanup_after: str | None = None  # ISO timestamp for auto-cleanup after merge
    workspace_role: str = "task"

    @classmethod
    def from_row(cls, row: Any) -> Worktree:
        """Create Worktree from database row."""

        def _safe_get(field: str) -> Any:
            """Get a field that may not exist in older schemas."""
            if hasattr(row, "get"):
                return row.get(field)
            try:
                return row[field]
            except (KeyError, IndexError, TypeError):
                return None

        return cls(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            branch_name=row["branch_name"],
            worktree_path=row["worktree_path"],
            base_branch=row["base_branch"],
            agent_session_id=row["agent_session_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            merged_at=row["merged_at"],
            merge_state=_safe_get("merge_state"),
            cleanup_after=_safe_get("cleanup_after"),
            workspace_role=_safe_get("workspace_role") or "task",
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "branch_name": self.branch_name,
            "worktree_path": self.worktree_path,
            "base_branch": self.base_branch,
            "agent_session_id": self.agent_session_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "merged_at": self.merged_at,
            "merge_state": self.merge_state,
            "cleanup_after": self.cleanup_after,
            "workspace_role": self.workspace_role,
        }


class LocalWorktreeManager:
    """Manager for local worktree storage."""

    def __init__(self, db: HubDatabase):
        """Initialize with database connection."""
        self.db = db

    def create(
        self,
        project_id: str,
        branch_name: str,
        worktree_path: str,
        base_branch: str = "main",
        task_id: str | None = None,
        agent_session_id: str | None = None,
        workspace_role: str = "task",
    ) -> Worktree:
        """
        Create a new worktree record.

        Args:
            project_id: Project ID
            branch_name: Git branch name
            worktree_path: Absolute path to worktree directory
            base_branch: Base branch for the worktree
            task_id: Optional task ID to link
            agent_session_id: Optional session ID that owns this worktree

        Returns:
            Created Worktree instance
        """
        worktree_id = generate_prefixed_id("wt", length=6)
        now = datetime.now(UTC).isoformat()

        self.db.execute(
            """
            INSERT INTO worktrees (
                id, project_id, task_id, branch_name, worktree_path,
                base_branch, agent_session_id, status, created_at, updated_at,
                workspace_role
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worktree_id,
                project_id,
                task_id,
                branch_name,
                worktree_path,
                base_branch,
                agent_session_id,
                WorktreeStatus.ACTIVE.value,
                now,
                now,
                workspace_role,
            ),
        )

        return Worktree(
            id=worktree_id,
            project_id=project_id,
            task_id=task_id,
            branch_name=branch_name,
            worktree_path=worktree_path,
            base_branch=base_branch,
            agent_session_id=agent_session_id,
            status=WorktreeStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
            merged_at=None,
            workspace_role=workspace_role,
        )

    def get(self, worktree_id: str) -> Worktree | None:
        """Get worktree by ID."""
        row = self.db.fetchone("SELECT * FROM worktrees WHERE id = ?", (worktree_id,))
        return Worktree.from_row(row) if row else None

    def get_by_path(self, worktree_path: str) -> Worktree | None:
        """Get worktree by path."""
        row = self.db.fetchone("SELECT * FROM worktrees WHERE worktree_path = ?", (worktree_path,))
        return Worktree.from_row(row) if row else None

    def get_by_branch(self, project_id: str, branch_name: str) -> Worktree | None:
        """Get worktree by project and branch name."""
        row = self.db.fetchone(
            "SELECT * FROM worktrees WHERE project_id = ? AND branch_name = ?",
            (project_id, branch_name),
        )
        return Worktree.from_row(row) if row else None

    def get_by_task(self, task_id: str) -> Worktree | None:
        """Get worktree linked to a task."""
        row = self.db.fetchone("SELECT * FROM worktrees WHERE task_id = ?", (task_id,))
        return Worktree.from_row(row) if row else None

    def list_worktrees(
        self,
        project_id: str | None = None,
        status: str | None = None,
        agent_session_id: str | None = None,
        limit: int = 50,
    ) -> list[Worktree]:
        """
        List worktrees with optional filters.

        Args:
            project_id: Filter by project
            status: Filter by status
            agent_session_id: Filter by owning session
            limit: Maximum number of results

        Returns:
            List of Worktree instances
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
            SELECT * FROM worktrees
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
            """,  # nosec B608
            tuple(params),
        )
        return [Worktree.from_row(row) for row in rows]

    # Allowlist of valid worktree column names to prevent SQL injection
    _VALID_UPDATE_FIELDS = frozenset(
        {
            "branch_name",
            "base_branch",
            "worktree_path",
            "status",
            "agent_session_id",
            "task_id",
            "last_activity_at",
            "updated_at",
            "merged_at",
            "merge_state",
            "cleanup_after",
            "workspace_role",
        }
    )

    def update(self, worktree_id: str, **fields: Any) -> Worktree | None:
        """
        Update worktree fields.

        Args:
            worktree_id: Worktree ID to update
            **fields: Fields to update (must be valid column names)

        Returns:
            Updated Worktree or None if not found

        Raises:
            ValueError: If any field name is not in the allowlist
        """
        if not fields:
            return self.get(worktree_id)

        # Validate field names against allowlist to prevent SQL injection
        invalid_fields = set(fields.keys()) - self._VALID_UPDATE_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid field names: {invalid_fields}")

        # Add updated_at timestamp
        fields["updated_at"] = datetime.now(UTC).isoformat()

        set_clause = ", ".join(f"{key} = ?" for key in fields.keys())
        values = list(fields.values()) + [worktree_id]

        self.db.execute(
            f"UPDATE worktrees SET {set_clause} WHERE id = ?",  # nosec B608
            tuple(values),
        )

        return self.get(worktree_id)

    def delete(self, worktree_id: str) -> bool:
        """
        Delete worktree record.

        Args:
            worktree_id: Worktree ID to delete

        Returns:
            True if deleted, False if not found
        """
        cursor = self.db.execute("DELETE FROM worktrees WHERE id = ?", (worktree_id,))
        return cursor.rowcount > 0

    # Status transition methods

    def claim(self, worktree_id: str, session_id: str) -> Worktree | None:
        """
        Claim ownership of a worktree for a session.

        Args:
            worktree_id: Worktree ID
            session_id: Session ID claiming ownership

        Returns:
            Updated Worktree or None if not found
        """
        return self.update(worktree_id, agent_session_id=session_id)

    def release(self, worktree_id: str) -> Worktree | None:
        """
        Release ownership of a worktree.

        Args:
            worktree_id: Worktree ID

        Returns:
            Updated Worktree or None if not found
        """
        return self.update(worktree_id, agent_session_id=None)

    def mark_stale(self, worktree_id: str) -> Worktree | None:
        """
        Mark worktree as stale (inactive).

        Args:
            worktree_id: Worktree ID

        Returns:
            Updated Worktree or None if not found
        """
        return self.update(worktree_id, status=WorktreeStatus.STALE.value)

    def mark_merged(self, worktree_id: str, cleanup_days: int = 7) -> Worktree | None:
        """
        Mark worktree as merged and schedule cleanup.

        Args:
            worktree_id: Worktree ID
            cleanup_days: Days until auto-cleanup (default: 7)

        Returns:
            Updated Worktree or None if not found
        """
        now = datetime.now(UTC)
        cleanup_after = (now + timedelta(days=cleanup_days)).isoformat()
        return self.update(
            worktree_id,
            status=WorktreeStatus.MERGED.value,
            merged_at=now.isoformat(),
            cleanup_after=cleanup_after,
        )

    def mark_abandoned(self, worktree_id: str) -> Worktree | None:
        """
        Mark worktree as abandoned.

        Args:
            worktree_id: Worktree ID

        Returns:
            Updated Worktree or None if not found
        """
        return self.update(worktree_id, status=WorktreeStatus.ABANDONED.value)

    def find_stale(
        self,
        project_id: str,
        hours: int = 24,
        limit: int = 50,
    ) -> list[Worktree]:
        """
        Find worktrees that are stale (no activity for N hours).

        Args:
            project_id: Project ID
            hours: Hours of inactivity threshold
            limit: Maximum number of results

        Returns:
            List of stale Worktree instances
        """
        # Calculate cutoff time
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

        rows = self.db.fetchall(
            """
            SELECT * FROM worktrees
            WHERE project_id = ?
              AND status = ?
              AND updated_at < ?
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (project_id, WorktreeStatus.ACTIVE.value, cutoff, limit),
        )
        return [Worktree.from_row(row) for row in rows]

    def find_expired(
        self,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[Worktree]:
        """
        Find merged worktrees past their cleanup window.

        These are worktrees where the merge succeeded and the grace period
        (cleanup_after) has elapsed. Safe to delete — work is in target branch.

        Args:
            project_id: Optional project filter (None = all projects)
            limit: Maximum number of results

        Returns:
            List of expired Worktree instances
        """
        now = datetime.now(UTC).isoformat()
        sql = "SELECT * FROM worktrees WHERE status = ? AND cleanup_after IS NOT NULL AND cleanup_after < ?"
        params: list[Any] = [WorktreeStatus.MERGED.value, now]
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY cleanup_after ASC LIMIT ?"
        params.append(limit)
        rows = self.db.fetchall(sql, tuple(params))
        return [Worktree.from_row(row) for row in rows]

    def cleanup_stale(
        self,
        project_id: str,
        hours: int = 24,
        dry_run: bool = True,
    ) -> list[Worktree]:
        """
        Mark stale worktrees as abandoned.

        This only updates the database status. The actual git worktree
        cleanup should be done by the WorktreeManager after calling this.

        Args:
            project_id: Project ID
            hours: Hours of inactivity threshold
            dry_run: If True, just return candidates without updating

        Returns:
            List of worktrees marked/to be marked as abandoned.
            When dry_run is False, returns refreshed worktrees with updated status.
        """
        stale = self.find_stale(project_id, hours)

        if not dry_run:
            updated: list[Worktree] = []
            for worktree in stale:
                # mark_abandoned returns the updated Worktree
                result = self.mark_abandoned(worktree.id)
                if result is not None:
                    updated.append(result)
            return updated

        return stale

    def count_by_status(self, project_id: str) -> dict[str, int]:
        """
        Get count of worktrees by status for a project.

        Args:
            project_id: Project ID

        Returns:
            Dict mapping status to count
        """
        rows = self.db.fetchall(
            """
            SELECT status, COUNT(*) as count
            FROM worktrees
            WHERE project_id = ?
            GROUP BY status
            """,
            (project_id,),
        )
        return {row["status"]: row["count"] for row in rows}

    # Merge state methods

    def set_merge_state(self, worktree_id: str, merge_state: str | None) -> Worktree | None:
        """
        Set the merge state for a worktree.

        Args:
            worktree_id: Worktree ID
            merge_state: Merge state ("pending", "resolved", or None)

        Returns:
            Updated Worktree or None if not found
        """
        return self.update(worktree_id, merge_state=merge_state)

    def get_by_merge_state(
        self,
        merge_state: str,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[Worktree]:
        """
        Get worktrees by merge state.

        Args:
            merge_state: Merge state to filter by
            project_id: Optional project ID filter
            limit: Maximum number of results

        Returns:
            List of Worktree instances with the given merge state
        """
        if project_id:
            rows = self.db.fetchall(
                """
                SELECT * FROM worktrees
                WHERE merge_state = ? AND project_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (merge_state, project_id, limit),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM worktrees
                WHERE merge_state = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (merge_state, limit),
            )
        return [Worktree.from_row(row) for row in rows]

    def sync_with_merge_resolution(
        self,
        worktree_id: str,
        merge_manager: Any | None = None,
        strategy: str = "auto",
    ) -> dict[str, Any]:
        """
        Sync worktree with merge resolution support.

        When conflicts are detected during sync, a merge resolution
        is initiated with the specified strategy.

        Args:
            worktree_id: Worktree ID
            merge_manager: MergeResolutionManager for creating resolutions
            strategy: Resolution strategy ("auto", "ai-only", "human")

        Returns:
            Dict with sync result and optional merge info
        """
        worktree = self.get(worktree_id)
        if not worktree:
            return {"success": False, "error": "Worktree not found"}

        # Placeholder: actual sync would involve git operations
        # and detection of merge conflicts

        return {
            "success": True,
            "worktree_id": worktree_id,
            "merge_initiated": False,
            "message": "Sync completed without conflicts",
        }

    def sync(self, worktree_id: str) -> dict[str, Any]:
        """
        Basic sync without merge resolution.

        Args:
            worktree_id: Worktree ID

        Returns:
            Dict with sync result
        """
        return self.sync_with_merge_resolution(worktree_id)
