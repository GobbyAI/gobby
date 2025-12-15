"""Local session storage manager."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gobby.storage.database import LocalDatabase

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """Session data model."""

    id: str
    external_id: str
    machine_id: str
    source: str
    project_id: str  # Required - sessions must belong to a project
    title: str | None
    status: str
    jsonl_path: str | None
    summary_path: str | None
    summary_markdown: str | None
    git_branch: str | None
    parent_session_id: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "Session":
        """Create Session from database row."""
        return cls(
            id=row["id"],
            external_id=row["external_id"],
            machine_id=row["machine_id"],
            source=row["source"],
            project_id=row["project_id"],
            title=row["title"],
            status=row["status"],
            jsonl_path=row["jsonl_path"],
            summary_path=row["summary_path"],
            summary_markdown=row["summary_markdown"],
            git_branch=row["git_branch"],
            parent_session_id=row["parent_session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "external_id": self.external_id,
            "machine_id": self.machine_id,
            "source": self.source,
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "jsonl_path": self.jsonl_path,
            "summary_path": self.summary_path,
            "summary_markdown": self.summary_markdown,
            "git_branch": self.git_branch,
            "parent_session_id": self.parent_session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class LocalSessionManager:
    """Manager for local session storage."""

    def __init__(self, db: LocalDatabase):
        """Initialize with database connection."""
        self.db = db

    def register(
        self,
        external_id: str,
        machine_id: str,
        source: str,
        project_id: str,
        title: str | None = None,
        jsonl_path: str | None = None,
        git_branch: str | None = None,
        parent_session_id: str | None = None,
    ) -> Session:
        """
        Register a new session or update existing one.

        Uses upsert to handle duplicate external_id/machine_id/source combinations.

        Args:
            external_id: External session identifier
            machine_id: Machine identifier
            source: CLI source (claude_code, codex, gemini)
            project_id: Project ID (required - sessions must belong to a project)
            title: Optional session title
            jsonl_path: Path to transcript file
            git_branch: Git branch name
            parent_session_id: Parent session for handoff

        Returns:
            Session instance
        """
        session_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        # Try insert, update on conflict
        self.db.execute(
            """
            INSERT INTO sessions (
                id, external_id, machine_id, source, project_id, title,
                jsonl_path, git_branch, parent_session_id,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(external_id, machine_id, source) DO UPDATE SET
                project_id = COALESCE(excluded.project_id, project_id),
                title = COALESCE(excluded.title, title),
                jsonl_path = COALESCE(excluded.jsonl_path, jsonl_path),
                git_branch = COALESCE(excluded.git_branch, git_branch),
                parent_session_id = COALESCE(excluded.parent_session_id, parent_session_id),
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                external_id,
                machine_id,
                source,
                project_id,
                title,
                jsonl_path,
                git_branch,
                parent_session_id,
                now,
                now,
            ),
        )

        # Return the session (either newly created or existing)
        return self.find_current(external_id, machine_id, source)  # type: ignore

    def get(self, session_id: str) -> Session | None:
        """Get session by ID."""
        row = self.db.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return Session.from_row(row) if row else None

    def find_current(
        self,
        external_id: str,
        machine_id: str,
        source: str,
    ) -> Session | None:
        """Find current session by external_id, machine_id, and source."""
        row = self.db.fetchone(
            """
            SELECT * FROM sessions
            WHERE external_id = ? AND machine_id = ? AND source = ?
            """,
            (external_id, machine_id, source),
        )
        return Session.from_row(row) if row else None

    def find_parent(
        self,
        machine_id: str,
        source: str,
        project_id: str,
        status: str = "handoff_ready",
    ) -> Session | None:
        """
        Find parent session for handoff.

        Finds the most recent session with handoff_ready status
        on the same machine, source, and project.
        """
        row = self.db.fetchone(
            """
            SELECT * FROM sessions
            WHERE machine_id = ? AND source = ? AND status = ? AND project_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (machine_id, source, status, project_id),
        )
        return Session.from_row(row) if row else None

    def update_status(self, session_id: str, status: str) -> Session | None:
        """Update session status."""
        now = datetime.utcnow().isoformat()
        self.db.execute(
            "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, session_id),
        )
        return self.get(session_id)

    def update_title(self, session_id: str, title: str) -> Session | None:
        """Update session title."""
        now = datetime.utcnow().isoformat()
        self.db.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, session_id),
        )
        return self.get(session_id)

    def update_summary(
        self,
        session_id: str,
        summary_path: str | None = None,
        summary_markdown: str | None = None,
    ) -> Session | None:
        """Update session summary."""
        now = datetime.utcnow().isoformat()
        self.db.execute(
            """
            UPDATE sessions
            SET summary_path = COALESCE(?, summary_path),
                summary_markdown = COALESCE(?, summary_markdown),
                updated_at = ?
            WHERE id = ?
            """,
            (summary_path, summary_markdown, now, session_id),
        )
        return self.get(session_id)

    def list(
        self,
        project_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        limit: int = 100,
    ) -> list[Session]:
        """
        List sessions with optional filters.

        Args:
            project_id: Filter by project
            status: Filter by status
            source: Filter by CLI source
            limit: Maximum number of results

        Returns:
            List of Session instances
        """
        conditions = []
        params: list[Any] = []

        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if source:
            conditions.append("source = ?")
            params.append(source)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        rows = self.db.fetchall(
            f"""
            SELECT * FROM sessions
            WHERE {where_clause}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [Session.from_row(row) for row in rows]

    def delete(self, session_id: str) -> bool:
        """Delete session by ID."""
        cursor = self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return bool(cursor.rowcount and cursor.rowcount > 0)
