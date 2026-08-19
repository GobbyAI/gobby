"""Local clone storage manager.

Manages local git clones for parallel development, distinct from worktrees.
Clones are full repository copies while worktrees share a single .git directory.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import psycopg

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workspace_machine_scope import (
    get_owned_workspace_row,
    raise_if_foreign_workspace,
    session_is_local,
)
from gobby.utils.datetime import (
    normalize_datetime_model,
    parse_stored_datetime,
    utc_now,
)
from gobby.utils.machine_id import require_machine_id

logger = logging.getLogger(__name__)


class CloneStatus(str, Enum):
    """Clone status values."""

    ACTIVE = "active"
    SYNCING = "syncing"
    STALE = "stale"
    MERGED = "merged"
    CLEANUP = "cleanup"
    DELETING = "deleting"


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=(
        "last_sync_at",
        "cleanup_after",
    ),
)
@dataclass
class Clone:
    """Clone data model."""

    id: str
    project_id: str
    machine_id: str = field(default_factory=require_machine_id, kw_only=True)
    branch_name: str | None
    clone_path: str
    base_branch: str
    task_id: str | None
    agent_session_id: str | None
    status: str
    remote_url: str | None
    last_sync_at: datetime | None
    cleanup_after: datetime | None
    created_at: datetime
    updated_at: datetime
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
            machine_id=str(row["machine_id"]),
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
            "machine_id": self.machine_id,
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
            "machine_id": self.machine_id,
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

    def __init__(self, db: HubDatabase):
        """Initialize with database connection."""
        self.db = db

    def create(
        self,
        project_id: str,
        branch_name: str | None,
        clone_path: str,
        base_branch: str = "main",
        task_id: str | None = None,
        agent_session_id: str | None = None,
        remote_url: str | None = None,
        cleanup_after: datetime | str | None = None,
        workspace_role: str = "task",
    ) -> Clone:
        """
        Create a new clone record.

        Args:
            project_id: Project ID
            branch_name: Git branch name, or None for a detached clone
            clone_path: Absolute path to clone directory
            base_branch: Base branch for the clone
            task_id: Optional task ID to link
            agent_session_id: Optional session ID that owns this clone
            remote_url: Optional remote URL of the repository
            cleanup_after: Optional ISO timestamp for automatic cleanup

        Returns:
            Created Clone instance
        """
        clone_id = str(uuid.uuid4())
        cleanup_after_value = parse_stored_datetime(cleanup_after)
        machine_id = require_machine_id()
        if agent_session_id and not session_is_local(
            self.db,
            agent_session_id,
            current_machine_id=machine_id,
        ):
            raise ValueError(f"Session not found: {agent_session_id}")

        row = self.db.execute(
            """
            INSERT INTO clones (
                id, project_id, machine_id, branch_name, clone_path, base_branch,
                task_id, agent_session_id, status, remote_url,
                last_sync_at, cleanup_after, workspace_role
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING created_at, updated_at
            """,
            (
                clone_id,
                project_id,
                machine_id,
                branch_name,
                clone_path,
                base_branch,
                task_id,
                agent_session_id,
                CloneStatus.ACTIVE.value,
                remote_url,
                None,  # last_sync_at
                cleanup_after_value,
                workspace_role,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Failed to create clone record: {clone_id}")

        return Clone(
            id=clone_id,
            project_id=project_id,
            machine_id=machine_id,
            branch_name=branch_name,
            clone_path=clone_path,
            base_branch=base_branch,
            task_id=task_id,
            agent_session_id=agent_session_id,
            status=CloneStatus.ACTIVE.value,
            remote_url=remote_url,
            last_sync_at=None,
            cleanup_after=cleanup_after_value,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            workspace_role=workspace_role,
        )

    def get(self, clone_id: str) -> Clone | None:
        """Get clone by ID."""
        clone = self._get_any(clone_id)
        if clone and clone.status == CloneStatus.CLEANUP.value:
            return None
        return clone

    def _get_any(self, clone_id: str) -> Clone | None:
        """Get a clone by ID, including terminal cleanup records."""
        row = get_owned_workspace_row(
            self.db,
            "clone",
            clone_id,
            current_machine_id=require_machine_id(),
        )
        return Clone.from_row(row) if row else None

    def get_by_task(self, task_id: str) -> Clone | None:
        """Get clone linked to a task."""
        row = self.db.fetchone(
            """
            SELECT * FROM clones
            WHERE task_id = %s AND machine_id = %s AND status != %s
            ORDER BY
                CASE status
                    WHEN %s THEN 0
                    WHEN %s THEN 1
                    WHEN %s THEN 2
                    WHEN %s THEN 3
                    WHEN %s THEN 4
                    ELSE 5
                END,
                updated_at DESC,
                created_at DESC
            LIMIT 1
            """,
            (
                task_id,
                require_machine_id(),
                CloneStatus.CLEANUP.value,
                CloneStatus.ACTIVE.value,
                CloneStatus.SYNCING.value,
                CloneStatus.STALE.value,
                CloneStatus.MERGED.value,
                CloneStatus.DELETING.value,
            ),
        )
        return Clone.from_row(row) if row else None

    def get_by_path(self, clone_path: str) -> Clone | None:
        """Get clone by path."""
        clone = self.get_by_path_any_status(clone_path)
        if clone and clone.status == CloneStatus.CLEANUP.value:
            return None
        return clone

    def get_by_path_any_status(self, clone_path: str) -> Clone | None:
        """Get a local clone by path, including terminal cleanup records."""
        row = self.db.fetchone(
            """SELECT * FROM clones
               WHERE clone_path = %s AND machine_id = %s""",
            (clone_path, require_machine_id()),
        )
        return Clone.from_row(row) if row else None

    def register_adopted(
        self,
        project_id: str,
        branch_name: str | None,
        clone_path: str,
        base_branch: str,
        remote_url: str | None,
    ) -> tuple[Clone, bool]:
        """Register inspected clone metadata, reviving cleanup tombstones safely."""
        existing = self.get_by_path_any_status(clone_path)
        if existing is not None:
            return self._reuse_adopted(
                existing,
                project_id=project_id,
                branch_name=branch_name,
                base_branch=base_branch,
                remote_url=remote_url,
            )

        try:
            return (
                self.create(
                    project_id=project_id,
                    branch_name=branch_name,
                    clone_path=clone_path,
                    base_branch=base_branch,
                    remote_url=remote_url,
                ),
                True,
            )
        except psycopg.IntegrityError:
            existing = self.get_by_path_any_status(clone_path)
            if existing is None:
                raise
            return self._reuse_adopted(
                existing,
                project_id=project_id,
                branch_name=branch_name,
                base_branch=base_branch,
                remote_url=remote_url,
            )

    def _reuse_adopted(
        self,
        existing: Clone,
        *,
        project_id: str,
        branch_name: str | None,
        base_branch: str,
        remote_url: str | None,
    ) -> tuple[Clone, bool]:
        if existing.project_id != project_id:
            raise ValueError(f"Clone path belongs to another project: {existing.clone_path}")
        if existing.status != CloneStatus.CLEANUP.value:
            return existing, False

        revived = self.update(
            existing.id,
            branch_name=branch_name,
            base_branch=base_branch,
            remote_url=remote_url,
            task_id=None,
            agent_session_id=None,
            status=CloneStatus.ACTIVE.value,
            cleanup_after=None,
            last_sync_at=None,
        )
        if revived is None:
            raise RuntimeError(f"Failed to revive clone record: {existing.id}")
        return revived, True

    def get_by_branch(self, project_id: str, branch_name: str) -> Clone | None:
        """Get clone by project and branch name."""
        row = self.db.fetchone(
            """
            SELECT * FROM clones
            WHERE project_id = %s AND branch_name = %s AND machine_id = %s AND status != %s
            """,
            (project_id, branch_name, require_machine_id(), CloneStatus.CLEANUP.value),
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
        conditions = ["status != %s", "machine_id = %s"]
        params: list[Any] = [CloneStatus.CLEANUP.value, require_machine_id()]

        if project_id:
            conditions.append("project_id = %s")
            params.append(project_id)
        if status:
            conditions.append("status = %s")
            params.append(status)
        if agent_session_id:
            conditions.append("agent_session_id = %s")
            params.append(agent_session_id)
        where_clause = " AND ".join(conditions)
        params.append(limit)

        query = f"SELECT * FROM clones WHERE {where_clause} "  # nosec
        query += "ORDER BY created_at DESC LIMIT %s"
        rows = self.db.fetchall(
            query,
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

        machine_id = require_machine_id()
        agent_session_id = fields.get("agent_session_id")
        if agent_session_id is not None and not session_is_local(
            self.db,
            str(agent_session_id),
            current_machine_id=machine_id,
        ):
            raise ValueError(f"Session not found: {agent_session_id}")

        for timestamp_field in ("last_sync_at", "cleanup_after", "updated_at"):
            if timestamp_field in fields:
                fields[timestamp_field] = parse_stored_datetime(fields[timestamp_field])

        # Add updated_at timestamp
        fields["updated_at"] = utc_now()

        set_clause = ", ".join(f"{key} = %s" for key in fields.keys())
        values = list(fields.values()) + [clone_id, machine_id]

        query = f"UPDATE clones SET {set_clause} WHERE id = %s AND machine_id = %s"  # nosec
        self.db.execute(query, tuple(values))

        return self._get_any(clone_id)

    def delete(self, clone_id: str) -> bool:
        """
        Delete clone record.

        Args:
            clone_id: Clone ID to delete

        Returns:
            True if deleted, False if not found
        """
        cursor = self.db.execute(
            "DELETE FROM clones WHERE id = %s AND machine_id = %s",
            (clone_id, machine_id := require_machine_id()),
        )
        if cursor.rowcount <= 0:
            raise_if_foreign_workspace(
                self.db,
                "clone",
                clone_id,
                current_machine_id=machine_id,
            )
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

    def mark_merged(
        self,
        clone_id: str,
        cleanup_after: datetime | str | None = None,
    ) -> Clone | None:
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
        now = utc_now()
        return self.update(
            clone_id,
            status=CloneStatus.ACTIVE.value,
            last_sync_at=now,
            cleanup_after=None,
        )

    def claim(self, clone_id: str, session_id: str) -> Clone | None:
        """
        Claim a clone for an agent session.

        Args:
            clone_id: Clone ID
            session_id: Session ID claiming ownership

        Returns:
            Updated Clone, or None if the clone is missing or owned by another session
        """
        machine_id = require_machine_id()
        if not session_is_local(self.db, session_id, current_machine_id=machine_id):
            return None
        cursor = self.db.execute(
            """
            UPDATE clones
            SET agent_session_id = %s, cleanup_after = NULL, updated_at = %s
            WHERE id = %s
              AND machine_id = %s
              AND (agent_session_id IS NULL OR agent_session_id = %s)
              AND status != %s
            """,
            (
                session_id,
                utc_now(),
                clone_id,
                machine_id,
                session_id,
                CloneStatus.CLEANUP.value,
            ),
        )
        if cursor.rowcount <= 0:
            raise_if_foreign_workspace(
                self.db,
                "clone",
                clone_id,
                current_machine_id=machine_id,
            )
            return None
        return self.get(clone_id)

    def release(self, clone_id: str) -> Clone | None:
        """
        Release a clone from its current owner.

        Args:
            clone_id: Clone ID

        Returns:
            Updated Clone or None if not found
        """
        return self.update(clone_id, agent_session_id=None)

    def count_by_status(self, project_id: str | None) -> dict[str, int]:
        """
        Get count of clones by status for a project.

        Args:
            project_id: Project ID (None matches no rows)

        Returns:
            Dict mapping status to count
        """
        rows = self.db.fetchall(
            """
            SELECT status, COUNT(*) as count
            FROM clones
            WHERE project_id = %s AND machine_id = %s AND status != %s
            GROUP BY status
            """,
            (project_id, require_machine_id(), CloneStatus.CLEANUP.value),
        )
        return {row["status"]: row["count"] for row in rows}

    def find_stale(
        self,
        project_id: str | None,
        hours: int = 24,
        limit: int = 50,
    ) -> list[Clone]:
        """
        Find clones that are stale (no activity for N hours).

        Finds active or interrupted-sync clones with no agent_session_id and
        updated_at older than the threshold.

        Args:
            project_id: Project ID (None matches no rows)
            hours: Hours of inactivity threshold
            limit: Maximum number of results

        Returns:
            List of stale Clone instances
        """
        cutoff = utc_now() - timedelta(hours=hours)

        rows = self.db.fetchall(
            """
            SELECT * FROM clones
            WHERE project_id = %s
              AND machine_id = %s
              AND status IN (%s, %s)
              AND agent_session_id IS NULL
              AND updated_at < %s
            ORDER BY updated_at ASC
            LIMIT %s
            """,
            (
                project_id,
                require_machine_id(),
                CloneStatus.ACTIVE.value,
                CloneStatus.SYNCING.value,
                cutoff,
                limit,
            ),
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
        now = utc_now()
        if project_id:
            rows = self.db.fetchall(
                """
                SELECT * FROM clones
                WHERE project_id = %s
                  AND machine_id = %s
                  AND status = %s
                  AND agent_session_id IS NULL
                  AND cleanup_after IS NOT NULL
                  AND cleanup_after < %s
                ORDER BY cleanup_after ASC
                LIMIT %s
                """,
                (project_id, require_machine_id(), CloneStatus.MERGED.value, now, limit),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM clones
                WHERE machine_id = %s
                  AND status = %s
                  AND agent_session_id IS NULL
                  AND cleanup_after IS NOT NULL
                  AND cleanup_after < %s
                ORDER BY cleanup_after ASC
                LIMIT %s
                """,
                (require_machine_id(), CloneStatus.MERGED.value, now, limit),
            )
        return [Clone.from_row(row) for row in rows]

    def cleanup_stale(
        self,
        project_id: str | None,
        hours: int = 24,
        dry_run: bool = True,
    ) -> list[Clone]:
        """
        Mark stale clones for cleanup.

        Args:
            project_id: Project ID (None matches no rows)
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
