"""Local project storage manager."""

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.config.bootstrap_io import default_gobby_home
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

ORPHANED_PROJECT_ID = "00000000-0000-0000-0000-000000000000"
PERSONAL_PROJECT_ID = "00000000-0000-0000-0000-000000060887"
GLOBAL_PROJECT_ID = "00000000-0000-0000-0000-000000000002"
SYSTEM_PROJECT_NAMES = frozenset({"_orphaned", "_migrated", "_personal", "_global", "gobby"})


def personal_project_path(gobby_home: Path | None = None) -> Path:
    """Return the local folder that backs the personal system project."""
    return (gobby_home or default_gobby_home()) / "personal"


def ensure_personal_project(db: HubDatabase, *, gobby_home: Path | None = None) -> "Project":
    """Ensure the `_personal` project has a real local folder and repo_path."""
    path = personal_project_path(gobby_home)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError as exc:
        logger.debug("Failed to chmod personal project path %s: %s", path, exc)

    project_manager = LocalProjectManager(db)
    project = project_manager.ensure_exists(PERSONAL_PROJECT_ID, "_personal", repo_path=str(path))
    if project.repo_path != str(path) or project.deleted_at is not None:
        now = datetime.now(UTC).isoformat()
        db.execute(
            """
            UPDATE projects
            SET repo_path = %s, deleted_at = NULL, updated_at = %s
            WHERE id = %s
              AND (repo_path IS DISTINCT FROM %s OR deleted_at IS NOT NULL)
            """,
            (str(path), now, PERSONAL_PROJECT_ID, str(path)),
        )
        project = project_manager.get(PERSONAL_PROJECT_ID) or project
    return project


@dataclass
class Project:
    """Project data model."""

    id: str
    name: str
    repo_path: str | None
    github_url: str | None
    created_at: str
    updated_at: str
    github_repo: str | None = None  # GitHub repo in "owner/repo" format
    linear_team_id: str | None = None  # Linear team ID for project sync
    linear_project_id: str | None = None  # Linear project ID for scoped sync
    linear_synced_at: str | None = None  # Last bidirectional Linear sync timestamp
    deleted_at: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Project":
        """Create Project from database row."""
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            repo_path=row["repo_path"],
            github_url=row["github_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            github_repo=row["github_repo"] if "github_repo" in keys else None,
            linear_team_id=row["linear_team_id"] if "linear_team_id" in keys else None,
            linear_project_id=row["linear_project_id"] if "linear_project_id" in keys else None,
            linear_synced_at=row["linear_synced_at"] if "linear_synced_at" in keys else None,
            deleted_at=row["deleted_at"] if "deleted_at" in keys else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "repo_path": self.repo_path,
            "github_url": self.github_url,
            "github_repo": self.github_repo,
            "linear_team_id": self.linear_team_id,
            "linear_project_id": self.linear_project_id,
            "linear_synced_at": self.linear_synced_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.deleted_at:
            d["deleted_at"] = self.deleted_at
        return d


class LocalProjectManager:
    """Manager for local project storage."""

    def __init__(self, db: HubDatabase):
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
        now = datetime.now(UTC).isoformat()

        self.db.execute(
            """
            INSERT INTO projects (id, name, repo_path, github_url, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
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
        row = self.db.fetchone("SELECT * FROM projects WHERE id = %s", (project_id,))
        return Project.from_row(row) if row else None

    def get_by_name(self, name: str, include_deleted: bool = False) -> Project | None:
        """Get project by name. Excludes soft-deleted projects by default."""
        if include_deleted:
            row = self.db.fetchone("SELECT * FROM projects WHERE name = %s", (name,))
        else:
            row = self.db.fetchone(
                "SELECT * FROM projects WHERE name = %s AND deleted_at IS NULL", (name,)
            )
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

    def ensure_exists(
        self,
        project_id: str,
        name: str,
        repo_path: str | None = None,
    ) -> Project:
        """
        Ensure a project with the given ID exists in the database.

        This is used when syncing projects from project.json files that may have
        been created on another machine. If the project doesn't exist, it's created
        with the specified ID.

        Args:
            project_id: The project ID (from project.json)
            name: Project name
            repo_path: Local repository path

        Returns:
            The existing or newly created Project
        """
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            INSERT INTO projects (id, name, repo_path, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (project_id, name, repo_path, now, now),
        )

        project = self.get(project_id)
        if project:
            return project

        raise RuntimeError(
            f"Project '{name}' ({project_id}) not found after idempotent insert — "
            "possible database inconsistency"
        )

    def list(self, include_deleted: bool = False) -> list[Project]:
        """List all projects. Excludes soft-deleted projects by default."""
        if include_deleted:
            rows = self.db.fetchall("SELECT * FROM projects ORDER BY name")
        else:
            rows = self.db.fetchall("SELECT * FROM projects WHERE deleted_at IS NULL ORDER BY name")
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

        allowed = {
            "name",
            "repo_path",
            "github_url",
            "github_repo",
            "linear_team_id",
            "linear_project_id",
            "linear_synced_at",
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return self.get(project_id)

        fields["updated_at"] = datetime.now(UTC).isoformat()

        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [project_id]

        self.db.execute(
            f"UPDATE projects SET {set_clause} WHERE id = %s",  # nosec B608
            tuple(values),
        )

        return self.get(project_id)

    def delete(self, project_id: str) -> bool:
        """
        Delete project by ID (hard delete).

        Returns:
            True if deleted, False if not found
        """
        cursor = self.db.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        return cursor.rowcount > 0

    def resolve_ref(self, ref: str) -> Project | None:
        """Resolve a project reference (UUID or name). Excludes deleted projects."""
        project = self.get(ref)
        if project and not project.deleted_at:
            return project
        return self.get_by_name(ref)

    def is_protected(self, project: Project) -> bool:
        """Check if a project is a protected system project."""
        return project.name in SYSTEM_PROJECT_NAMES

    def soft_delete(self, project_id: str) -> bool:
        """Soft-delete a project by setting deleted_at timestamp.

        Returns:
            True if updated, False if not found
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            "UPDATE projects SET deleted_at = %s, updated_at = %s WHERE id = %s AND deleted_at IS NULL",
            (now, now, project_id),
        )
        return cursor.rowcount > 0
