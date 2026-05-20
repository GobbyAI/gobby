"""
Merge resolution storage module.

Stores merge resolutions and conflicts for worktree merge operations.
"""

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.id import generate_prefixed_id

logger = logging.getLogger(__name__)


class ConflictStatus(Enum):
    """Status of a merge conflict."""

    PENDING = "pending"
    RESOLVED = "resolved"
    FAILED = "failed"
    HUMAN_REVIEW = "human_review"


@dataclass
class MergeResolution:
    """A merge resolution record tracking a merge operation."""

    id: str
    worktree_id: str
    source_branch: str
    target_branch: str
    status: str
    tier_used: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MergeResolution":
        """Create a MergeResolution from a database row."""
        return cls(
            id=row["id"],
            worktree_id=row["worktree_id"],
            source_branch=row["source_branch"],
            target_branch=row["target_branch"],
            status=row["status"],
            tier_used=row["tier_used"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert resolution to dictionary for serialization."""
        return {
            "id": self.id,
            "worktree_id": self.worktree_id,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "status": self.status,
            "tier_used": self.tier_used,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class MergeConflict:
    """A merge conflict record for a specific file."""

    id: str
    resolution_id: str
    file_path: str
    status: str
    ours_content: str | None
    theirs_content: str | None
    resolved_content: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MergeConflict":
        """Create a MergeConflict from a database row."""
        return cls(
            id=row["id"],
            resolution_id=row["resolution_id"],
            file_path=row["file_path"],
            status=row["status"],
            ours_content=row["ours_content"],
            theirs_content=row["theirs_content"],
            resolved_content=row["resolved_content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert conflict to dictionary for serialization."""
        return {
            "id": self.id,
            "resolution_id": self.resolution_id,
            "file_path": self.file_path,
            "status": self.status,
            "ours_content": self.ours_content,
            "theirs_content": self.theirs_content,
            "resolved_content": self.resolved_content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MergeResolutionManager:
    """Manages merge resolutions and conflicts in local SQLite database."""

    def __init__(self, db: HubDatabase):
        self.db = db
        self._change_listeners: list[Callable[[], Any]] = []

    def add_change_listener(self, listener: Callable[[], Any]) -> None:
        """Add a change listener that will be called on create/update/delete."""
        self._change_listeners.append(listener)

    def _notify_listeners(self) -> None:
        """Notify all change listeners."""
        for listener in self._change_listeners:
            try:
                listener()
            except Exception as e:
                logger.error(f"Error in merge resolution change listener: {e}")

    # =========================================================================
    # Resolution CRUD
    # =========================================================================

    def create_resolution(
        self,
        worktree_id: str,
        source_branch: str,
        target_branch: str,
        status: str = "pending",
        tier_used: str | None = None,
    ) -> MergeResolution:
        """Create a new merge resolution.

        Args:
            worktree_id: ID of the worktree
            source_branch: Branch being merged in
            target_branch: Target branch (e.g., main)
            status: Resolution status (default: pending)
            tier_used: Resolution tier used (if resolved)

        Returns:
            The created MergeResolution
        """
        now = datetime.now(UTC).isoformat()
        resolution_id = generate_prefixed_id("mr", worktree_id + source_branch)

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO merge_resolutions (
                    id, worktree_id, source_branch, target_branch,
                    status, tier_used, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolution_id,
                    worktree_id,
                    source_branch,
                    target_branch,
                    status,
                    tier_used,
                    now,
                    now,
                ),
            )

        self._notify_listeners()
        result = self.get_resolution(resolution_id)
        if result is None:
            raise RuntimeError(
                f"Failed to retrieve resolution '{resolution_id}' after successful insert"
            )
        return result

    def get_resolution_for_merge(
        self,
        worktree_id: str,
        source_branch: str,
        target_branch: str,
    ) -> MergeResolution | None:
        """Get the newest resolution for an exact merge request."""
        row = self.db.fetchone(
            """
            SELECT * FROM merge_resolutions
            WHERE worktree_id = ? AND source_branch = ? AND target_branch = ?
            ORDER BY created_at DESC, updated_at DESC
            LIMIT 1
            """,
            (worktree_id, source_branch, target_branch),
        )
        return MergeResolution.from_row(row) if row else None

    def get_or_create_resolution(
        self,
        worktree_id: str,
        source_branch: str,
        target_branch: str,
        status: str = "pending",
        tier_used: str | None = None,
    ) -> tuple[MergeResolution, bool]:
        """Return an exact existing resolution or create a new one.

        Returns:
            A tuple of ``(resolution, created)``. Duplicate insert races are
            treated as reuse only when the existing row is an exact merge match.
        """
        existing = self.get_resolution_for_merge(
            worktree_id=worktree_id,
            source_branch=source_branch,
            target_branch=target_branch,
        )
        if existing:
            return existing, False

        try:
            return (
                self.create_resolution(
                    worktree_id=worktree_id,
                    source_branch=source_branch,
                    target_branch=target_branch,
                    status=status,
                    tier_used=tier_used,
                ),
                True,
            )
        except sqlite3.IntegrityError:
            existing = self.get_resolution_for_merge(
                worktree_id=worktree_id,
                source_branch=source_branch,
                target_branch=target_branch,
            )
            if existing:
                return existing, False
            raise

    def get_resolution(self, resolution_id: str) -> MergeResolution | None:
        """Get a resolution by ID.

        Args:
            resolution_id: The resolution ID

        Returns:
            The MergeResolution if found, None otherwise
        """
        row = self.db.fetchone("SELECT * FROM merge_resolutions WHERE id = ?", (resolution_id,))
        if not row:
            return None
        return MergeResolution.from_row(row)

    def update_resolution(
        self,
        resolution_id: str,
        status: str | None = None,
        tier_used: str | None = None,
    ) -> MergeResolution | None:
        """Update a resolution.

        Args:
            resolution_id: The resolution ID
            status: New status (optional)
            tier_used: New tier used (optional)

        Returns:
            The updated MergeResolution if found, None otherwise
        """
        resolution = self.get_resolution(resolution_id)
        if not resolution:
            return None

        now = datetime.now(UTC).isoformat()
        new_status = status if status is not None else resolution.status
        new_tier = tier_used if tier_used is not None else resolution.tier_used

        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE merge_resolutions
                SET status = ?, tier_used = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_status, new_tier, now, resolution_id),
            )

        self._notify_listeners()
        return self.get_resolution(resolution_id)

    def delete_resolution(self, resolution_id: str) -> bool:
        """Delete a resolution by ID.

        Args:
            resolution_id: The resolution ID to delete

        Returns:
            True if deleted, False if not found
        """
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM merge_resolutions WHERE id = ?", (resolution_id,))
            if cursor.rowcount == 0:
                return False

        self._notify_listeners()
        return True

    def list_resolutions(
        self,
        worktree_id: str | None = None,
        source_branch: str | None = None,
        target_branch: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MergeResolution]:
        """List resolutions with optional filters.

        Args:
            worktree_id: Filter by worktree ID
            source_branch: Filter by source branch
            target_branch: Filter by target branch
            status: Filter by status
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            List of matching MergeResolutions
        """
        query = "SELECT * FROM merge_resolutions WHERE 1=1"
        params: list[Any] = []

        if worktree_id:
            query += " AND worktree_id = ?"
            params.append(worktree_id)

        if source_branch:
            query += " AND source_branch = ?"
            params.append(source_branch)

        if target_branch:
            query += " AND target_branch = ?"
            params.append(target_branch)

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.db.fetchall(query, tuple(params))
        return [MergeResolution.from_row(row) for row in rows]

    # =========================================================================
    # Conflict CRUD
    # =========================================================================

    def create_conflict(
        self,
        resolution_id: str,
        file_path: str,
        ours_content: str | None = None,
        theirs_content: str | None = None,
        status: str = "pending",
    ) -> MergeConflict:
        """Create a new merge conflict.

        Args:
            resolution_id: ID of the parent resolution
            file_path: Path to the conflicting file
            ours_content: Content from our side
            theirs_content: Content from their side
            status: Conflict status (default: pending)

        Returns:
            The created MergeConflict
        """
        now = datetime.now(UTC).isoformat()
        conflict_id = generate_prefixed_id("mc", resolution_id + file_path)

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO merge_conflicts (
                    id, resolution_id, file_path, status,
                    ours_content, theirs_content, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conflict_id,
                    resolution_id,
                    file_path,
                    status,
                    ours_content,
                    theirs_content,
                    now,
                    now,
                ),
            )

        self._notify_listeners()
        result = self.get_conflict(conflict_id)
        if result is None:
            raise RuntimeError(
                f"Failed to retrieve conflict '{conflict_id}' after successful insert"
            )
        return result

    def get_conflict(self, conflict_id: str) -> MergeConflict | None:
        """Get a conflict by ID.

        Args:
            conflict_id: The conflict ID

        Returns:
            The MergeConflict if found, None otherwise
        """
        row = self.db.fetchone("SELECT * FROM merge_conflicts WHERE id = ?", (conflict_id,))
        if not row:
            return None
        return MergeConflict.from_row(row)

    def update_conflict(
        self,
        conflict_id: str,
        status: str | None = None,
        resolved_content: str | None = None,
    ) -> MergeConflict | None:
        """Update a conflict.

        Args:
            conflict_id: The conflict ID
            status: New status (optional)
            resolved_content: Resolved content (optional)

        Returns:
            The updated MergeConflict if found, None otherwise
        """
        conflict = self.get_conflict(conflict_id)
        if not conflict:
            return None

        now = datetime.now(UTC).isoformat()
        new_status = status if status is not None else conflict.status
        new_resolved = (
            resolved_content if resolved_content is not None else conflict.resolved_content
        )

        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE merge_conflicts
                SET status = ?, resolved_content = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_status, new_resolved, now, conflict_id),
            )

        self._notify_listeners()
        return self.get_conflict(conflict_id)

    def delete_conflict(self, conflict_id: str) -> bool:
        """Delete a conflict by ID.

        Args:
            conflict_id: The conflict ID to delete

        Returns:
            True if deleted, False if not found
        """
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM merge_conflicts WHERE id = ?", (conflict_id,))
            if cursor.rowcount == 0:
                return False

        self._notify_listeners()
        return True

    def list_conflicts(
        self,
        resolution_id: str | None = None,
        file_path: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MergeConflict]:
        """List conflicts with optional filters.

        Args:
            resolution_id: Filter by resolution ID
            file_path: Filter by file path
            status: Filter by status
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            List of matching MergeConflicts
        """
        query = "SELECT * FROM merge_conflicts WHERE 1=1"
        params: list[Any] = []

        if resolution_id:
            query += " AND resolution_id = ?"
            params.append(resolution_id)

        if file_path:
            query += " AND file_path = ?"
            params.append(file_path)

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.db.fetchall(query, tuple(params))
        return [MergeConflict.from_row(row) for row in rows]

    # =========================================================================
    # Helper Methods for CLI
    # =========================================================================

    def get_active_resolution(self, worktree_id: str | None = None) -> MergeResolution | None:
        """
        Get the current active (pending) merge resolution.

        Args:
            worktree_id: Optional worktree ID to filter by

        Returns:
            The most recent pending MergeResolution, or None
        """
        if worktree_id:
            row = self.db.fetchone(
                """
                SELECT * FROM merge_resolutions
                WHERE worktree_id = ? AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (worktree_id,),
            )
        else:
            row = self.db.fetchone(
                """
                SELECT * FROM merge_resolutions
                WHERE status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        return MergeResolution.from_row(row) if row else None

    def get_latest_resolution(self, worktree_id: str) -> MergeResolution | None:
        """Get the most recently updated merge resolution for a worktree."""
        row = self.db.fetchone(
            """
            SELECT * FROM merge_resolutions
            WHERE worktree_id = ?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (worktree_id,),
        )
        return MergeResolution.from_row(row) if row else None

    def get_conflict_by_path(
        self, file_path: str, resolution_id: str | None = None
    ) -> MergeConflict | None:
        """
        Get a conflict by file path.

        Args:
            file_path: Path to the conflicting file
            resolution_id: Optional resolution ID to filter by

        Returns:
            The MergeConflict if found, None otherwise
        """
        if resolution_id:
            row = self.db.fetchone(
                """
                SELECT * FROM merge_conflicts
                WHERE file_path = ? AND resolution_id = ?
                """,
                (file_path, resolution_id),
            )
        else:
            # Find any pending conflict with this path
            row = self.db.fetchone(
                """
                SELECT c.* FROM merge_conflicts c
                JOIN merge_resolutions r ON c.resolution_id = r.id
                WHERE c.file_path = ? AND r.status = 'pending'
                ORDER BY c.created_at DESC
                LIMIT 1
                """,
                (file_path,),
            )
        return MergeConflict.from_row(row) if row else None

    def has_active_resolution_for_worktree(self, worktree_id: str) -> bool:
        """
        Check if a worktree has an active (pending) merge resolution.

        Args:
            worktree_id: Worktree ID to check

        Returns:
            True if an active resolution exists
        """
        return self.get_active_resolution(worktree_id) is not None
