"""Local project storage manager."""

import json
import logging
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import normalize_datetime_model, utc_now
from gobby.utils.session_context import get_current_session_id
from gobby.utils.uuid_validation import parse_uuid_reference

logger = logging.getLogger(__name__)

ORPHANED_PROJECT_ID = "00000000-0000-0000-0000-000000000000"
MIGRATED_PROJECT_ID = "00000000-0000-0000-0000-000000000001"
PERSONAL_PROJECT_ID = "00000000-0000-0000-0000-000000060887"
GLOBAL_PROJECT_ID = "00000000-0000-0000-0000-000000000002"
CHECKOUT_FREE_PROJECT_IDS = frozenset(
    {
        ORPHANED_PROJECT_ID,
        MIGRATED_PROJECT_ID,
        GLOBAL_PROJECT_ID,
        PERSONAL_PROJECT_ID,
    }
)
SYSTEM_PROJECT_NAMES = frozenset({"_orphaned", "_migrated", "_personal", "_global", "gobby"})


class IsolatedAgentProjectPathError(ValueError):
    """Raised when an isolated agent session tries to set a canonical repo path."""


class NameAttachRejectedError(ValueError):
    """Raised when init tries to attach a checkout by project name instead of marker id."""


class AmbiguousProjectRefError(ValueError):
    """Raised when a project name matches more than one unique row."""


def personal_project_path(gobby_home: Path | None = None) -> Path:
    """Hub-owner _personal directory. Raises FilesHomeNotOnThisDaemonError on a node."""
    del gobby_home
    from gobby.paths import require_files_home

    return require_files_home() / "_personal"


def ensure_personal_project_identity(
    *,
    gobby_home: Path | None = None,
    created_at: datetime | None = None,
) -> Path:
    """Ensure the personal workspace has a valid DB-independent project marker."""
    del gobby_home
    from gobby.paths import publish_files_home_descendant, require_files_home
    from gobby.runner_pid_file import held_singleton_claim

    if held_singleton_claim() is None:
        raise RuntimeError("filesystem identity requires a held singleton")

    root = require_files_home()
    path = root / "_personal"
    try:
        path.chmod(0o700)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Failed to chmod personal project path %s: %s", path, exc)

    project_file = path / ".gobby" / "project.json"
    data: Any = None
    if project_file.exists():
        try:
            data = json.loads(project_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Rewriting corrupt personal project.json %s: %s", project_file, exc)
        if _is_valid_personal_identity(data):
            return project_file
        logger.warning(
            "Repairing personal project.json %s: expected id %s and name _personal",
            project_file,
            PERSONAL_PROJECT_ID,
        )

    existing_created_at = data.get("created_at") if isinstance(data, dict) else None
    marker_created_at = (
        created_at.isoformat()
        if created_at is not None
        else existing_created_at
        if _is_iso_datetime(existing_created_at)
        else utc_now().isoformat()
    )
    payload = {
        "id": PERSONAL_PROJECT_ID,
        "name": "_personal",
        "created_at": marker_created_at,
    }
    publish_files_home_descendant(
        Path("_personal") / ".gobby" / "project.json",
        (json.dumps(payload, indent=2) + "\n").encode("utf-8"),
    )

    repaired = json.loads(project_file.read_text())
    if not _is_valid_personal_identity(repaired):
        raise RuntimeError(f"Failed to establish personal project identity at {project_file}")
    return project_file


def _is_valid_personal_identity(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and data.get("id") == PERSONAL_PROJECT_ID
        and data.get("name") == "_personal"
        and _is_iso_datetime(data.get("created_at"))
    )


def _is_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def ensure_personal_project(db: HubDatabase, *, gobby_home: Path | None = None) -> "Project":
    """Upsert the checkout-free `_personal` sentinel. Identity is local-owner only."""
    del gobby_home
    from gobby.config.bootstrap import load_bootstrap
    from gobby.runner_pid_file import held_singleton_claim

    config = load_bootstrap()
    write_identity = (
        config.datastore_mode == "local"
        and bool(config.files_home)
        and held_singleton_claim() is not None
    )
    project_manager = LocalProjectManager(db)
    now = utc_now()
    with db.transaction() as txn:
        txn.execute(
            """
            INSERT INTO projects (id, name, deleted_at)
            VALUES (%s, %s, NULL)
            ON CONFLICT (id) DO UPDATE
            SET name = EXCLUDED.name,
                deleted_at = NULL,
                updated_at = EXCLUDED.updated_at
            """,
            (PERSONAL_PROJECT_ID, "_personal"),
        )
    project = project_manager.get(PERSONAL_PROJECT_ID)
    if project is None:
        raise RuntimeError("Personal project not found after transactional upsert")
    if write_identity:
        ensure_personal_project_identity(created_at=project.created_at or now)
    return project


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=(
        "linear_synced_at",
        "deleted_at",
    ),
)
@dataclass
class Project:
    """Project data model."""

    id: str
    name: str
    github_url: str | None
    created_at: datetime
    updated_at: datetime
    github_repo: str | None = None  # GitHub repo in "owner/repo" format
    linear_team_id: str | None = None  # Linear team ID for project sync
    linear_project_id: str | None = None  # Linear project ID for scoped sync
    linear_synced_at: datetime | None = None  # Last bidirectional Linear sync timestamp
    linear_sync_enabled: bool = False  # Daemon-managed Linear reconciliation
    deleted_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Project":
        """Create Project from database row."""
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            github_url=row["github_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            github_repo=row["github_repo"] if "github_repo" in keys else None,
            linear_team_id=row["linear_team_id"] if "linear_team_id" in keys else None,
            linear_project_id=row["linear_project_id"] if "linear_project_id" in keys else None,
            linear_synced_at=row["linear_synced_at"] if "linear_synced_at" in keys else None,
            linear_sync_enabled=(
                bool(row["linear_sync_enabled"]) if "linear_sync_enabled" in keys else False
            ),
            deleted_at=row["deleted_at"] if "deleted_at" in keys else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "github_url": self.github_url,
            "github_repo": self.github_repo,
            "linear_team_id": self.linear_team_id,
            "linear_project_id": self.linear_project_id,
            "linear_synced_at": self.linear_synced_at,
            "linear_sync_enabled": self.linear_sync_enabled,
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

    def _is_isolated_agent_session(self) -> bool:
        session_id = get_current_session_id() or os.environ.get("GOBBY_SESSION_ID")
        if not session_id:
            return False
        row = self.db.fetchone(
            """
            SELECT 1
            FROM agent_runs
            WHERE child_session_id = %s
              AND (worktree_id IS NOT NULL OR clone_id IS NOT NULL)
            LIMIT 1
            """,
            (session_id,),
        )
        return row is not None

    def _is_registered_isolation_path(self, repo_path: str | None, *, machine_id: str) -> bool:
        if not repo_path:
            return False
        row = self.db.fetchone(
            """
            SELECT 1 FROM worktrees WHERE machine_id = %s AND worktree_path = %s
            UNION ALL
            SELECT 1 FROM clones WHERE machine_id = %s AND clone_path = %s
            LIMIT 1
            """,
            (machine_id, repo_path, machine_id, repo_path),
        )
        if row is not None:
            return True

        try:
            canonical_path = Path(repo_path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return True

        rows = self.db.fetchall(
            """
            SELECT worktree_path AS isolation_path FROM worktrees WHERE machine_id = %s
            UNION ALL
            SELECT clone_path AS isolation_path FROM clones WHERE machine_id = %s
            """,
            (machine_id, machine_id),
        )
        for registered in rows:
            try:
                registered_path = (
                    Path(registered["isolation_path"]).expanduser().resolve(strict=False)
                )
            except (OSError, RuntimeError):
                continue
            if registered_path == canonical_path:
                return True
        return False

    @staticmethod
    def _is_under_isolation_root(repo_path: str | None) -> bool:
        # An orphaned worktree/clone directory (dir exists, registry row gone)
        # must not become a project's primary checkout, so the roots are
        # forbidden prefixes regardless of registration state.
        if not repo_path:
            return False
        try:
            canonical_path = Path(repo_path).expanduser().resolve(strict=False)
            gobby_home = Path.home() / ".gobby"
            isolation_roots = tuple(
                (gobby_home / name).resolve(strict=False) for name in ("worktrees", "clones")
            )
        except (OSError, RuntimeError):
            return True
        for root in isolation_roots:
            if canonical_path.is_relative_to(root):
                return True
        return False

    def _repo_path_write_is_blocked(self, repo_path: str | None, *, machine_id: str) -> bool:
        return (
            self._is_isolated_agent_session()
            or self._is_under_isolation_root(repo_path)
            or self._is_registered_isolation_path(repo_path, machine_id=machine_id)
        )

    def _guard_repo_path_write(self, repo_path: str | None, *, machine_id: str) -> None:
        if self._repo_path_write_is_blocked(repo_path, machine_id=machine_id):
            raise IsolatedAgentProjectPathError(
                "project repo_path cannot be changed from an isolated agent session "
                "or to an isolation path (registered or under the worktrees/clones roots)"
            )

    def _validated_ordinary_root(
        self,
        project_id: str,
        repo_path: str,
        *,
        machine_id: str | None,
    ) -> tuple[str, str]:
        from gobby.storage.workspace_machine_scope import require_local_machine_id
        from gobby.utils.checkout_root import validate_checkout_root

        local_machine_id = require_local_machine_id(
            machine_id, resource_kind="project_checkout", resource_id=project_id
        )
        root = validate_checkout_root(
            self.db,
            project_id=project_id,
            machine_id=local_machine_id,
            candidate_path=repo_path,
            expected_marker_id=project_id,
        )
        return local_machine_id, root

    def _write_checkout(self, machine_id: str, project_id: str, root_path: str) -> None:
        from gobby.storage.project_checkouts import LocalProjectCheckoutManager

        manager = LocalProjectCheckoutManager(self.db)
        existing = manager.get(machine_id, project_id)
        if existing is None:
            manager.register(machine_id, project_id, root_path)
        else:
            manager.rebind(machine_id, project_id, root_path)

    def create(
        self,
        name: str,
        repo_path: str | None = None,
        github_url: str | None = None,
        *,
        machine_id: str | None = None,
        project_id: str | None = None,
    ) -> Project:
        """
        Create a new project.

        Args:
            name: Unique project name
            repo_path: Local filesystem path for this machine's checkout
            github_url: GitHub repository URL
            machine_id: Claimed machine id, or None for the local daemon
            project_id: Stable marker UUID, or None to generate one

        Returns:
            Created Project instance
        """
        resolved_id = project_id or str(uuid.uuid4())
        checkout: tuple[str, str] | None = None
        if repo_path is not None:
            checkout = self._validated_ordinary_root(resolved_id, repo_path, machine_id=machine_id)

        if checkout is None:
            row = self.db.fetchone(
                """
                INSERT INTO projects (id, name, github_url)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (resolved_id, name, github_url),
            )
            if row is None:
                raise RuntimeError(f"Project '{name}' not found after insert")
            return Project.from_row(row)

        with self.db.transaction():
            row = self.db.fetchone(
                """
                INSERT INTO projects (id, name, github_url)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (resolved_id, name, github_url),
            )
            if row is None:
                raise RuntimeError(f"Project '{name}' not found after insert")
            from gobby.storage.project_checkouts import LocalProjectCheckoutManager

            LocalProjectCheckoutManager(self.db).register(checkout[0], resolved_id, checkout[1])
        return Project.from_row(row)

    def get(self, project_id: str) -> Project | None:
        """Get project by ID."""
        if parse_uuid_reference(project_id) is None:
            return None
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
        *,
        machine_id: str | None = None,
    ) -> Project:
        """Get existing project or create a new one.

        A checkout root never attaches an existing name. Name-only calls still
        upsert only the logical project row.
        """
        if repo_path is not None:
            existing = self.get_by_name(name, include_deleted=True)
            if existing is not None:
                raise NameAttachRejectedError(
                    f"project name {name!r} already exists; init is marker-authoritative"
                )
            return self.create(
                name, repo_path=repo_path, github_url=github_url, machine_id=machine_id
            )

        project_id = str(uuid.uuid4())
        row = self.db.fetchone(
            """
            INSERT INTO projects (id, name, github_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (name) WHERE deleted_at IS NULL
            DO UPDATE SET name = EXCLUDED.name
            RETURNING *
            """,
            (project_id, name, github_url),
        )
        if row is None:
            raise RuntimeError(f"Project '{name}' not found after atomic upsert")
        return Project.from_row(row)

    def ensure_exists(
        self,
        project_id: str,
        name: str,
        repo_path: str | None = None,
        *,
        machine_id: str | None = None,
    ) -> Project:
        """
        Ensure a project with the given ID exists in the database.

        This is used when syncing projects from project.json files that may have
        been created on another machine. If the project doesn't exist, it's created
        with the specified ID.

        Args:
            project_id: The project ID (from project.json)
            name: Project name
            repo_path: Local filesystem path for this machine's checkout
            machine_id: Claimed machine id, or None for the local daemon

        Returns:
            The existing or newly created Project
        """
        checkout: tuple[str, str] | None = None
        if repo_path is not None:
            from gobby.storage.workspace_machine_scope import require_local_machine_id

            local_machine_id = require_local_machine_id(
                machine_id, resource_kind="project_checkout", resource_id=project_id
            )
            if self._repo_path_write_is_blocked(repo_path, machine_id=local_machine_id):
                project = self.get(project_id)
                if project is None:
                    raise IsolatedAgentProjectPathError(
                        "isolated agent session cannot establish a canonical project checkout"
                    )
                return project
            checkout = self._validated_ordinary_root(project_id, repo_path, machine_id=machine_id)

        self.db.execute(
            """
            INSERT INTO projects (id, name)
            VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE SET
                updated_at = now()
            """,
            (project_id, name),
        )
        if checkout is not None:
            from gobby.storage.project_checkouts import LocalProjectCheckoutManager

            LocalProjectCheckoutManager(self.db).register(checkout[0], project_id, checkout[1])

        project = self.get(project_id)
        if project:
            return project

        raise RuntimeError(f"Project '{name}' ({project_id}) not found after ID-targeted upsert")

    def list(self, include_deleted: bool = False) -> list[Project]:
        """List all projects. Excludes soft-deleted projects by default."""
        if include_deleted:
            rows = self.db.fetchall("SELECT * FROM projects ORDER BY name")
        else:
            rows = self.db.fetchall("SELECT * FROM projects WHERE deleted_at IS NULL ORDER BY name")
        return [Project.from_row(row) for row in rows]

    def list_purge_candidates(self, cutoff: datetime) -> Sequence[Project]:
        """List soft-deleted, non-system projects whose retention window elapsed."""
        rows = self.db.fetchall(
            """
            SELECT * FROM projects
            WHERE deleted_at IS NOT NULL
              AND deleted_at <= %s
              AND name <> ALL(%s)
            ORDER BY deleted_at, id
            """,
            (cutoff, list(SYSTEM_PROJECT_NAMES)),
        )
        return [Project.from_row(row) for row in rows]

    def list_page(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> Sequence[Project]:
        """List one stable, bounded page of projects."""
        if limit <= 0:
            raise ValueError(f"limit must be > 0, got {limit}")
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset}")

        if include_deleted:
            rows = self.db.fetchall(
                "SELECT * FROM projects ORDER BY name, id LIMIT %s OFFSET %s",
                (limit, offset),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM projects
                WHERE deleted_at IS NULL
                ORDER BY name, id
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
        return [Project.from_row(row) for row in rows]

    def update(
        self,
        project_id: str,
        *,
        machine_id: str | None = None,
        **fields: Any,
    ) -> Project | None:
        """
        Update project fields.

        Args:
            project_id: Project ID
            machine_id: Claimed machine id, or None for the local daemon
            **fields: Fields to update, plus an optional validated checkout root

        Returns:
            Updated Project or None if not found
        """
        repo_path = fields.pop("repo_path", None) if "repo_path" in fields else None
        checkout: tuple[str, str] | None = None
        if repo_path is not None:
            checkout = self._validated_ordinary_root(project_id, repo_path, machine_id=machine_id)

        allowed = {
            "name",
            "github_url",
            "github_repo",
            "linear_team_id",
            "linear_project_id",
            "linear_synced_at",
            "linear_sync_enabled",
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields and checkout is None:
            return self.get(project_id)

        if self.get(project_id) is None:
            return None

        if fields:
            fields["updated_at"] = utc_now()
            set_clause = ", ".join(f"{k} = %s" for k in fields)
            values = list(fields.values()) + [project_id]
            self.db.execute(
                # set_clause contains only fixed allowlisted project columns.
                f"UPDATE projects SET {set_clause} WHERE id = %s",  # nosec
                tuple(values),
            )
        if checkout is not None:
            self._write_checkout(checkout[0], project_id, checkout[1])

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
        now = utc_now()
        cursor = self.db.execute(
            "UPDATE projects SET deleted_at = %s, updated_at = %s WHERE id = %s AND deleted_at IS NULL",
            (now, now, project_id),
        )
        return cursor.rowcount > 0

    def restore(self, project_id: str) -> Project | None:
        """Restore a soft-deleted project."""
        now = utc_now()
        self.db.execute(
            "UPDATE projects SET deleted_at = NULL, updated_at = %s WHERE id = %s",
            (now, project_id),
        )
        return self.get(project_id)
