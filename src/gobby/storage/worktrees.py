"""Local worktree storage manager."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable, Mapping
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
from gobby.utils.datetime import normalize_datetime_model, utc_now
from gobby.utils.machine_id import require_machine_id
from gobby.utils.uuid_validation import parse_uuid_reference

logger = logging.getLogger(__name__)

# A prefix of a canonical UUID's text form: hex digits and dashes only.
_UUID_TEXT_PREFIX_RE = re.compile(r"[0-9a-fA-F-]+")


def _escape_like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class WorktreeStatus(str, Enum):
    """Worktree status values."""

    ACTIVE = "active"
    STALE = "stale"
    MERGED = "merged"
    ABANDONED = "abandoned"


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=(
        "last_activity_at",
        "merged_at",
        "cleanup_after",
    ),
)
@dataclass
class Worktree:
    """Worktree data model."""

    id: str
    project_id: str
    machine_id: str = field(default_factory=require_machine_id, kw_only=True)
    task_id: str | None
    branch_name: str | None
    worktree_path: str
    base_branch: str
    agent_session_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime | None = None
    merged_at: datetime | None = None
    merge_state: str | None = None  # "pending", "resolved", or None
    cleanup_after: datetime | None = None  # ISO timestamp for auto-cleanup after merge
    workspace_role: str = "task"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Worktree:
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
            machine_id=str(row["machine_id"]),
            task_id=row["task_id"],
            branch_name=row["branch_name"],
            worktree_path=row["worktree_path"],
            base_branch=row["base_branch"],
            agent_session_id=row["agent_session_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_activity_at=_safe_get("last_activity_at"),
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
            "machine_id": self.machine_id,
            "task_id": self.task_id,
            "branch_name": self.branch_name,
            "worktree_path": self.worktree_path,
            "base_branch": self.base_branch,
            "agent_session_id": self.agent_session_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activity_at": self.last_activity_at,
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
        branch_name: str | None,
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
            branch_name: Git branch name, or None for a detached worktree
            worktree_path: Absolute path to worktree directory
            base_branch: Base branch for the worktree
            task_id: Optional task ID to link
            agent_session_id: Optional session ID that owns this worktree

        Returns:
            Created Worktree instance
        """
        worktree_id = str(uuid.uuid4())
        now = utc_now()
        machine_id = require_machine_id()
        if agent_session_id and not session_is_local(
            self.db,
            agent_session_id,
            current_machine_id=machine_id,
        ):
            raise ValueError(f"Session not found: {agent_session_id}")

        row = self.db.execute(
            """
            INSERT INTO worktrees (
                id, project_id, machine_id, task_id, branch_name, worktree_path,
                base_branch, agent_session_id, status,
                last_activity_at, workspace_role
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING created_at, updated_at
            """,
            (
                worktree_id,
                project_id,
                machine_id,
                task_id,
                branch_name,
                worktree_path,
                base_branch,
                agent_session_id,
                WorktreeStatus.ACTIVE.value,
                now,
                workspace_role,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("Worktree insert returned no row")

        return Worktree(
            id=worktree_id,
            project_id=project_id,
            machine_id=machine_id,
            task_id=task_id,
            branch_name=branch_name,
            worktree_path=worktree_path,
            base_branch=base_branch,
            agent_session_id=agent_session_id,
            status=WorktreeStatus.ACTIVE.value,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_activity_at=now,
            merged_at=None,
            workspace_role=workspace_role,
        )

    def resolve_reference(self, ref: str) -> str:
        """Resolve a worktree reference (full UUID or unique id prefix) to its UUID.

        A full UUID is returned canonicalised without a lookup so ``get`` keeps
        owning the not-found and foreign-machine outcomes. Anything else is an
        id prefix: exactly one match resolves, more than one is ambiguous, and
        no match is not found. Text that cannot prefix a UUID (anything beyond
        hex digits and dashes) is not found without a query. The prefix never
        reaches the uuid column, so psycopg's ``invalid input syntax for type
        uuid`` cannot surface.

        Raises:
            ValueError: If the reference is ambiguous or matches no worktree.
        """
        ref = ref.strip()
        uuid_obj = parse_uuid_reference(ref)
        if uuid_obj is not None:
            return str(uuid_obj)
        if _UUID_TEXT_PREFIX_RE.fullmatch(ref) is None:
            raise ValueError(f"Worktree '{ref}' not found")

        rows = self.db.fetchall(
            "SELECT id FROM worktrees WHERE id::text LIKE %s ESCAPE '\\' ORDER BY id LIMIT 2",
            (f"{_escape_like_prefix(ref)}%",),
        )
        if len(rows) > 1:
            matches = ", ".join(str(row["id"]) for row in rows)
            raise ValueError(
                f"Ambiguous worktree reference '{ref}' matches: {matches}. "
                "Pass the full worktree UUID to disambiguate."
            )
        if not rows:
            raise ValueError(f"Worktree '{ref}' not found")
        return str(rows[0]["id"])

    def get(self, worktree_id: str) -> Worktree | None:
        """Get worktree by ID."""
        row = get_owned_workspace_row(
            self.db,
            "worktree",
            worktree_id,
            current_machine_id=require_machine_id(),
        )
        return Worktree.from_row(row) if row else None

    def get_by_path(self, worktree_path: str) -> Worktree | None:
        """Get worktree by path."""
        row = self.db.fetchone(
            "SELECT * FROM worktrees WHERE worktree_path = %s AND machine_id = %s",
            (worktree_path, require_machine_id()),
        )
        return Worktree.from_row(row) if row else None

    def has_path_on_other_machine(self, worktree_path: str) -> bool:
        """Return whether another machine owns a record for this filesystem path."""
        row = self.db.fetchone(
            """SELECT 1 FROM worktrees
               WHERE worktree_path = %s AND machine_id != %s
               LIMIT 1""",
            (worktree_path, require_machine_id()),
        )
        return row is not None

    def register_adopted(
        self,
        project_id: str,
        branch_name: str | None,
        worktree_path: str,
        base_branch: str,
    ) -> tuple[Worktree, bool]:
        """Register an inspected worktree, collapsing same-path insertion races."""
        if self.has_path_on_other_machine(worktree_path):
            raise ValueError(f"Worktree path is registered on another machine: {worktree_path}")

        existing = self.get_by_path(worktree_path)
        if existing is not None:
            if existing.project_id != project_id:
                raise ValueError(f"Worktree path belongs to another project: {worktree_path}")
            return existing, False

        try:
            return (
                self.create(
                    project_id=project_id,
                    branch_name=branch_name,
                    worktree_path=worktree_path,
                    base_branch=base_branch,
                ),
                True,
            )
        except psycopg.IntegrityError:
            existing = self.get_by_path(worktree_path)
            if existing is None:
                raise
            if existing.project_id != project_id:
                raise ValueError(
                    f"Worktree path belongs to another project: {worktree_path}"
                ) from None
            return existing, False

    def get_by_branch(self, project_id: str, branch_name: str) -> Worktree | None:
        """Get worktree by project and branch name."""
        row = self.db.fetchone(
            """SELECT * FROM worktrees
               WHERE project_id = %s AND branch_name = %s AND machine_id = %s""",
            (project_id, branch_name, require_machine_id()),
        )
        return Worktree.from_row(row) if row else None

    def get_by_task(self, task_id: str) -> Worktree | None:
        """Get worktree linked to a task."""
        row = self.db.fetchone(
            """
            SELECT * FROM worktrees
            WHERE task_id = %s
              AND machine_id = %s
            ORDER BY
                CASE status
                    WHEN %s THEN 0
                    WHEN %s THEN 1
                    WHEN %s THEN 2
                    WHEN %s THEN 3
                    ELSE 4
                END,
                updated_at DESC,
                created_at DESC
            LIMIT 1
            """,
            (
                task_id,
                require_machine_id(),
                WorktreeStatus.ACTIVE.value,
                WorktreeStatus.STALE.value,
                WorktreeStatus.MERGED.value,
                WorktreeStatus.ABANDONED.value,
            ),
        )
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
        conditions = ["machine_id = %s"]
        params: list[Any] = [require_machine_id()]

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

        rows = self.db.fetchall(
            f"""
            SELECT * FROM worktrees
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT %s
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

        machine_id = require_machine_id()
        agent_session_id = fields.get("agent_session_id")
        if agent_session_id is not None and not session_is_local(
            self.db,
            str(agent_session_id),
            current_machine_id=machine_id,
        ):
            raise ValueError(f"Session not found: {agent_session_id}")

        # Add updated_at timestamp
        fields["updated_at"] = utc_now()

        set_clause = ", ".join(f"{key} = %s" for key in fields.keys())
        values = list(fields.values()) + [worktree_id, machine_id]

        self.db.execute(
            f"UPDATE worktrees SET {set_clause} WHERE id = %s AND machine_id = %s",  # nosec B608
            tuple(values),
        )

        return self.get(worktree_id)

    def touch(self, worktree_id: str) -> Worktree | None:
        """Refresh a worktree's activity timestamp."""
        now = utc_now()
        machine_id = require_machine_id()
        self.db.execute(
            """UPDATE worktrees SET last_activity_at = %s, updated_at = %s
               WHERE id = %s AND machine_id = %s""",
            (now, now, worktree_id, machine_id),
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
        machine_id = require_machine_id()
        current = self.get(worktree_id)
        workspace_path = getattr(current, "worktree_path", None) if current is not None else None
        with self.db.transaction() as conn:
            if workspace_path:
                conn.execute(
                    """
                    UPDATE sessions
                    SET workspace_path = NULL,
                        workspace_generation = workspace_generation + 1,
                        updated_at = NOW()
                    WHERE workspace_path = %s
                    """,
                    (workspace_path,),
                )
            cursor = conn.execute(
                "DELETE FROM worktrees WHERE id = %s AND machine_id = %s",
                (worktree_id, machine_id),
            )
            deleted = cursor.rowcount > 0
        if not deleted:
            raise_if_foreign_workspace(
                self.db,
                "worktree",
                worktree_id,
                current_machine_id=machine_id,
            )
        return deleted

    # Status transition methods

    def claim(self, worktree_id: str, session_id: str) -> Worktree | None:
        """
        Claim ownership of a worktree for a session.

        Args:
            worktree_id: Worktree ID
            session_id: Session ID claiming ownership

        Returns:
            Updated Worktree, or None if the worktree is missing or owned by another session
        """
        return self.claim_if_available(
            worktree_id,
            session_id,
            allowed_existing_session_ids=(None, session_id),
        )

    def is_claimed_by_live_session(self, worktree_id: str) -> bool:
        """Return True when the worktree owner is an active session."""
        machine_id = require_machine_id()
        owned = get_owned_workspace_row(
            self.db,
            "worktree",
            worktree_id,
            current_machine_id=machine_id,
        )
        if owned is None:
            return False
        row = self.db.fetchone(
            """
            SELECT 1
            FROM worktrees wt
            JOIN sessions s ON s.id = wt.agent_session_id
            WHERE wt.id = %s AND wt.machine_id = %s AND s.status IN ('active', 'paused')
            """,
            (worktree_id, machine_id),
        )
        return row is not None

    def claim_if_available(
        self,
        worktree_id: str,
        session_id: str,
        *,
        allowed_existing_session_ids: Iterable[str | None] = (None,),
    ) -> Worktree | None:
        """Claim a worktree only if it is unowned or owned by an allowed prior session."""
        machine_id = require_machine_id()
        if not session_is_local(self.db, session_id, current_machine_id=machine_id):
            return None
        allowed = [value for value in allowed_existing_session_ids if value]
        conditions = ["id = %s", "machine_id = %s", "(agent_session_id IS NULL"]
        now = utc_now()
        params: list[Any] = [session_id, now, now, worktree_id, machine_id]
        if allowed:
            placeholders = ", ".join("%s" for _ in allowed)
            conditions[-1] += f" OR agent_session_id IN ({placeholders})"
            params.extend(allowed)
        conditions[-1] += ")"

        cursor = self.db.execute(
            f"""
            UPDATE worktrees
            SET agent_session_id = %s, last_activity_at = %s, updated_at = %s
            WHERE {" AND ".join(conditions)}
            """,  # nosec B608
            tuple(params),
        )
        if getattr(cursor, "rowcount", 0) <= 0:
            raise_if_foreign_workspace(
                self.db,
                "worktree",
                worktree_id,
                current_machine_id=machine_id,
            )
            return None
        return self.get(worktree_id)

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

    def mark_merged(self, worktree_id: str, cleanup_days: int = 0) -> Worktree | None:
        """
        Mark worktree as merged and schedule cleanup.

        Args:
            worktree_id: Worktree ID
            cleanup_days: Days until auto-cleanup. Defaults to immediate cleanup because merged
                build worktrees are already represented by the target branch and task records.

        Returns:
            Updated Worktree or None if not found
        """
        now = utc_now()
        cleanup_after = now + timedelta(days=cleanup_days)
        return self.update(
            worktree_id,
            status=WorktreeStatus.MERGED.value,
            merged_at=now,
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
        cutoff = utc_now() - timedelta(hours=hours)

        rows = self.db.fetchall(
            """
            SELECT * FROM worktrees
            WHERE project_id = %s
              AND machine_id = %s
              AND status = %s
              AND agent_session_id IS NULL
              AND COALESCE(last_activity_at, updated_at) < %s
            ORDER BY COALESCE(last_activity_at, updated_at) ASC
            LIMIT %s
            """,
            (project_id, require_machine_id(), WorktreeStatus.ACTIVE.value, cutoff, limit),
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
        now = utc_now()
        sql = """
            SELECT * FROM worktrees
            WHERE status = %s
              AND machine_id = %s
              AND agent_session_id IS NULL
              AND cleanup_after IS NOT NULL
              AND cleanup_after < %s
        """
        params: list[Any] = [WorktreeStatus.MERGED.value, require_machine_id(), now]
        if project_id:
            sql += " AND project_id = %s"
            params.append(project_id)
        sql += " ORDER BY cleanup_after ASC LIMIT %s"
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
            WHERE project_id = %s AND machine_id = %s
            GROUP BY status
            """,
            (project_id, require_machine_id()),
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
                WHERE merge_state = %s AND project_id = %s AND machine_id = %s
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (merge_state, project_id, require_machine_id(), limit),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM worktrees
                WHERE merge_state = %s AND machine_id = %s
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (merge_state, require_machine_id(), limit),
            )
        return [Worktree.from_row(row) for row in rows]
