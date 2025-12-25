"""Local project storage manager."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gobby.storage.database import LocalDatabase

logger = logging.getLogger(__name__)


@dataclass
class Project:
    """Project data model."""

    id: str
    name: str
    repo_path: str | None
    github_url: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "Project":
        """Create Project from database row."""
        return cls(
            id=row["id"],
            name=row["name"],
            repo_path=row["repo_path"],
            github_url=row["github_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "repo_path": self.repo_path,
            "github_url": self.github_url,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class LocalProjectManager:
    """Manager for local project storage."""

    def __init__(self, db: LocalDatabase):
        """Initialize with database connection."""
        self.db = db

    def create(
        self,
        name: str,
        repo_path: str | None = None,
        github_url: str | None = None,
    ) -> Project:
        """
        Create a new project.

        Args:
            name: Unique project name
            repo_path: Local repository path
            github_url: GitHub repository URL

        Returns:
            Created Project instance
        """
        project_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        self.db.execute(
            """
            INSERT INTO projects (id, name, repo_path, github_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, name, repo_path, github_url, now, now),
        )

        return Project(
            id=project_id,
            name=name,
            repo_path=repo_path,
            github_url=github_url,
            created_at=now,
            updated_at=now,
        )

    def get(self, project_id: str) -> Project | None:
        """Get project by ID."""
        row = self.db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        return Project.from_row(row) if row else None

    def get_by_name(self, name: str) -> Project | None:
        """Get project by name."""
        row = self.db.fetchone("SELECT * FROM projects WHERE name = ?", (name,))
        return Project.from_row(row) if row else None

    def get_or_create(
        self,
        name: str,
        repo_path: str | None = None,
        github_url: str | None = None,
    ) -> Project:
        """Get existing project or create new one."""
        project = self.get_by_name(name)
        if project:
            return project
        return self.create(name, repo_path, github_url)

    def list(self) -> list[Project]:
        """List all projects."""
        rows = self.db.fetchall("SELECT * FROM projects ORDER BY name")
        return [Project.from_row(row) for row in rows]

    def update(self, project_id: str, **fields: Any) -> Project | None:
        """
        Update project fields.

        Args:
            project_id: Project ID
            **fields: Fields to update (name, repo_path, github_url)

        Returns:
            Updated Project or None if not found
        """
        if not fields:
            return self.get(project_id)

        allowed = {"name", "repo_path", "github_url"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return self.get(project_id)

        fields["updated_at"] = datetime.utcnow().isoformat()

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [project_id]

        self.db.execute(
            f"UPDATE projects SET {set_clause} WHERE id = ?",
            tuple(values),
        )

        return self.get(project_id)

    def delete(self, project_id: str) -> bool:
        """
        Delete project by ID.

        Returns:
            True if deleted, False if not found
        """
        cursor = self.db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0  # type: ignore[no-any-return]
