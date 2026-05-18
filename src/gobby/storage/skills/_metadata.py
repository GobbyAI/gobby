"""Skill metadata CRUD operations (create, get, list, update, delete)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from gobby.storage.skills._models import Skill, SkillSourceType
from gobby.utils.id import generate_prefixed_id

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

_UNSET: Any = object()


class _SkillMetadataHost(Protocol):
    db: DatabaseProtocol

    def _notify_change(
        self,
        event_type: str,
        skill_id: str,
        skill_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def delete_skill_files(self, skill_id: str) -> int: ...

    def restore_skill_files(self, skill_id: str) -> int: ...


class SkillMetadataMixin:
    """Mixin providing skill metadata CRUD operations.

    Requires ``self.db`` (DatabaseProtocol) and ``self._notify_change()``.
    """

    db: DatabaseProtocol

    def _host(self) -> _SkillMetadataHost:
        return cast(_SkillMetadataHost, self)

    def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.db.transaction() as conn:
            row = conn.execute(query, params).fetchone()
            return cast(sqlite3.Row | None, row)

    def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.db.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
            return cast(list[sqlite3.Row], rows)

    def create_skill(
        self,
        name: str,
        description: str,
        content: str,
        version: str | None = None,
        license: str | None = None,
        compatibility: str | None = None,
        allowed_tools: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source_path: str | None = None,
        source_type: SkillSourceType | None = None,
        source_ref: str | None = None,
        hub_name: str | None = None,
        hub_slug: str | None = None,
        hub_version: str | None = None,
        enabled: bool = True,
        always_apply: bool = False,
        injection_format: str = "summary",
        project_id: str | None = None,
        source: str = "installed",
    ) -> Skill:
        """Create a new skill.

        Args:
            name: Skill name (max 64 chars, lowercase+hyphens)
            description: Skill description (max 1024 chars)
            content: Full markdown content
            version: Optional version string
            license: Optional license identifier
            compatibility: Optional compatibility notes (max 500 chars)
            allowed_tools: Optional list of allowed tool patterns
            metadata: Optional free-form metadata
            source_path: Original file path or URL
            source_type: Source type ('local', 'github', 'url', 'zip', 'filesystem')
            source_ref: Git ref for updates
            hub_name: Optional hub name
            hub_slug: Optional hub slug
            hub_version: Optional hub version
            enabled: Whether skill is active
            always_apply: Whether skill should always be advertised at session start
            injection_format: Manifest selection format (summary, full, content)
            project_id: Project scope (None for global)
            source: 'installed' or 'project' (default 'installed').
                Auto-set to 'project' when project_id is provided.

        Returns:
            The created Skill

        Raises:
            ValueError: If a skill with the same name and source exists in scope
        """
        # Auto-set source to 'project' for project-scoped skills
        if project_id is not None:
            source = "project"

        now = datetime.now(UTC).isoformat()
        skill_id = generate_prefixed_id("skl", f"{name}:{project_id or 'global'}:{source}")

        # Check if skill already exists in this project scope with same source
        existing = self.get_by_name(
            name, project_id=project_id, source=source, include_deleted=True
        )
        if existing:
            raise ValueError(
                f"Skill '{name}' (source={source}) already exists"
                + (f" in project {project_id}" if project_id else " globally")
            )

        # Serialize JSON fields
        allowed_tools_json = json.dumps(allowed_tools) if allowed_tools else None
        metadata_json = json.dumps(metadata) if metadata else None

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO skills (
                    id, name, description, content, version, license,
                    compatibility, allowed_tools, metadata, source_path,
                    source_type, source_ref, hub_name, hub_slug, hub_version,
                    enabled, always_apply, injection_format, project_id,
                    source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_id,
                    name,
                    description,
                    content,
                    version,
                    license,
                    compatibility,
                    allowed_tools_json,
                    metadata_json,
                    source_path,
                    source_type,
                    source_ref,
                    hub_name,
                    hub_slug,
                    hub_version,
                    enabled,
                    always_apply,
                    injection_format,
                    project_id,
                    source,
                    now,
                    now,
                ),
            )

        skill = self.get_skill(skill_id)
        self._host()._notify_change("create", skill_id, name)
        return skill

    def get_skill(self, skill_id: str, include_deleted: bool = False) -> Skill:
        """Get a skill by ID.

        Args:
            skill_id: The skill ID
            include_deleted: If True, include soft-deleted skills.

        Returns:
            The Skill

        Raises:
            ValueError: If skill not found
        """
        if include_deleted:
            row = self._fetchone("SELECT * FROM skills WHERE id = ?", (skill_id,))
        else:
            row = self._fetchone(
                "SELECT * FROM skills WHERE id = ? AND deleted_at IS NULL",
                (skill_id,),
            )
        if not row:
            raise ValueError(f"Skill {skill_id} not found")
        return Skill.from_row(row)

    def get_skills_by_ids(self, skill_ids: list[str]) -> list[Skill]:
        """Get multiple skills by ID in a single query.

        Args:
            skill_ids: List of skill IDs to fetch.

        Returns:
            List of found Skills (missing/deleted IDs are silently skipped).
        """
        if not skill_ids:
            return []
        placeholders = ",".join("?" * len(skill_ids))
        rows = self._fetchall(
            f"SELECT * FROM skills WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            tuple(skill_ids),
        )
        return [Skill.from_row(row) for row in rows]

    def get_by_name(
        self,
        name: str,
        project_id: str | None = None,
        include_global: bool = True,
        include_deleted: bool = False,
        source: str | None = None,
    ) -> Skill | None:
        """Get a skill by name within a project scope.

        By default returns only non-deleted skills, matching the
        workflow_definitions pattern. When an installed copy exists
        it shadows the template.

        Args:
            name: The skill name
            project_id: Project scope (None for global)
            include_global: Include global skills when project_id is set.
            include_deleted: If True, include soft-deleted skills.
            source: If set, filter to this exact source value.

        Returns:
            The Skill if found, None otherwise
        """
        # Build WHERE clause
        conditions = ["name = ?"]
        params: list[Any] = [name]

        if not include_deleted:
            conditions.append("deleted_at IS NULL")

        if source is not None:
            conditions.append("source = ?")
            params.append(source)

        where = " AND ".join(conditions)

        if project_id:
            # First try project-scoped skill
            row = self._fetchone(
                f"SELECT * FROM skills WHERE {where} AND project_id = ?",  # nosec B608
                (*params, project_id),
            )
            # If not found and include_global, try global
            if row is None and include_global:
                row = self._fetchone(
                    f"SELECT * FROM skills WHERE {where} AND project_id IS NULL",  # nosec B608
                    tuple(params),
                )
        else:
            row = self._fetchone(
                f"SELECT * FROM skills WHERE {where} AND project_id IS NULL",  # nosec B608
                tuple(params),
            )
        return Skill.from_row(row) if row else None

    def update_skill(
        self,
        skill_id: str,
        name: str | None = None,
        description: str | None = None,
        content: str | None = None,
        version: str | None = _UNSET,
        license: str | None = _UNSET,
        compatibility: str | None = _UNSET,
        allowed_tools: list[str] | None = _UNSET,
        metadata: dict[str, Any] | None = _UNSET,
        source_path: str | None = _UNSET,
        source_type: SkillSourceType | None = _UNSET,
        source_ref: str | None = _UNSET,
        hub_name: str | None = _UNSET,
        hub_slug: str | None = _UNSET,
        hub_version: str | None = _UNSET,
        enabled: bool | None = None,
        always_apply: bool | None = None,
        injection_format: str | None = None,
        source: str | None = None,
        project_id: str | None = _UNSET,
    ) -> Skill:
        """Update an existing skill.

        Args:
            skill_id: The skill ID to update
            name: New name (optional)
            description: New description (optional)
            content: New content (optional)
            version: New version (use _UNSET to leave unchanged, None to clear)
            license: New license (use _UNSET to leave unchanged, None to clear)
            compatibility: New compatibility (use _UNSET to leave unchanged, None to clear)
            allowed_tools: New allowed tools (use _UNSET to leave unchanged, None to clear)
            metadata: New metadata (use _UNSET to leave unchanged, None to clear)
            source_path: New source path (use _UNSET to leave unchanged, None to clear)
            source_type: New source type (use _UNSET to leave unchanged, None to clear)
            source_ref: New source ref (use _UNSET to leave unchanged, None to clear)
            hub_name: New hub name (use _UNSET to leave unchanged, None to clear)
            hub_slug: New hub slug (use _UNSET to leave unchanged, None to clear)
            hub_version: New hub version (use _UNSET to leave unchanged, None to clear)
            enabled: New enabled state (optional)
            always_apply: New always_apply state (optional)
            injection_format: New injection format (optional)
            source: New source value ('installed', 'project') (optional)
            project_id: New project_id (use _UNSET to leave unchanged, None to clear)

        Returns:
            The updated Skill

        Raises:
            ValueError: If skill not found
        """
        updates = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if version is not _UNSET:
            updates.append("version = ?")
            params.append(version)
        if license is not _UNSET:
            updates.append("license = ?")
            params.append(license)
        if compatibility is not _UNSET:
            updates.append("compatibility = ?")
            params.append(compatibility)
        if allowed_tools is not _UNSET:
            updates.append("allowed_tools = ?")
            params.append(json.dumps(allowed_tools) if allowed_tools else None)
        if metadata is not _UNSET:
            updates.append("metadata = ?")
            params.append(json.dumps(metadata) if metadata else None)
        if source_path is not _UNSET:
            updates.append("source_path = ?")
            params.append(source_path)
        if source_type is not _UNSET:
            updates.append("source_type = ?")
            params.append(source_type)
        if source_ref is not _UNSET:
            updates.append("source_ref = ?")
            params.append(source_ref)
        if hub_name is not _UNSET:
            updates.append("hub_name = ?")
            params.append(hub_name)
        if hub_slug is not _UNSET:
            updates.append("hub_slug = ?")
            params.append(hub_slug)
        if hub_version is not _UNSET:
            updates.append("hub_version = ?")
            params.append(hub_version)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(enabled)
        if always_apply is not None:
            updates.append("always_apply = ?")
            params.append(always_apply)
        if injection_format is not None:
            updates.append("injection_format = ?")
            params.append(injection_format)
        if source is not None:
            updates.append("source = ?")
            params.append(source)
        if project_id is not _UNSET:
            updates.append("project_id = ?")
            params.append(project_id)

        if not updates:
            return self.get_skill(skill_id)

        updates.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())
        params.append(skill_id)

        sql = f"UPDATE skills SET {', '.join(updates)} WHERE id = ?"  # nosec B608

        with self.db.transaction() as conn:
            cursor = conn.execute(sql, tuple(params))
            if cursor.rowcount == 0:
                raise ValueError(f"Skill {skill_id} not found")

        skill = self.get_skill(skill_id)
        self._host()._notify_change("update", skill_id, skill.name)
        return skill

    def delete_skill(self, skill_id: str) -> bool:
        """Soft-delete a skill by ID (sets deleted_at).

        Args:
            skill_id: The skill ID to delete

        Returns:
            True if deleted, False if not found
        """
        try:
            skill = self.get_skill(skill_id)
            skill_name = skill.name
        except ValueError:
            return False

        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE skills SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (now, now, skill_id),
            )
            if cursor.rowcount == 0:
                return False

        host = self._host()
        host.delete_skill_files(skill_id)
        host._notify_change("delete", skill_id, skill_name)
        return True

    def hard_delete(self, skill_id: str) -> bool:
        """Permanently delete a skill by ID.

        Args:
            skill_id: The skill ID to delete

        Returns:
            True if deleted, False if not found
        """
        try:
            skill = self.get_skill(skill_id, include_deleted=True)
            skill_name = skill.name
        except ValueError:
            return False

        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
            if cursor.rowcount == 0:
                return False

        self._host()._notify_change("delete", skill_id, skill_name)
        return True

    def restore(self, skill_id: str) -> Skill:
        """Restore a soft-deleted skill.

        Args:
            skill_id: The skill ID to restore

        Returns:
            The restored Skill

        Raises:
            ValueError: If skill not found
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE skills SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (now, skill_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Skill {skill_id} not found")

        host = self._host()
        host.restore_skill_files(skill_id)
        skill = self.get_skill(skill_id)
        host._notify_change("create", skill_id, skill.name)
        return skill

    def move_to_project(self, skill_id: str, project_id: str) -> Skill:
        """Move a skill to project scope.

        Args:
            skill_id: The skill ID
            project_id: Target project ID

        Returns:
            The updated Skill

        Raises:
            ValueError: If skill not found.
        """
        self.get_skill(skill_id)
        return self.update_skill(skill_id, source="project", project_id=project_id)

    def move_to_installed(self, skill_id: str) -> Skill:
        """Move a project-scoped skill back to installed scope.

        Args:
            skill_id: The skill ID

        Returns:
            The updated Skill

        Raises:
            ValueError: If skill not found.
        """
        self.get_skill(skill_id)
        return self.update_skill(skill_id, source="installed", project_id=None)

    def list_skills(
        self,
        project_id: str | None = None,
        enabled: bool | None = None,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_global: bool = True,
        include_deleted: bool = False,
        source: str | None = None,
    ) -> list[Skill]:
        """List skills with optional filtering.

        By default excludes soft-deleted skills.

        Args:
            project_id: Filter by project (None for global only)
            enabled: Filter by enabled state
            category: Filter by category (from metadata.skillport.category)
            limit: Maximum number of results
            offset: Number of results to skip
            include_global: Include global skills when project_id is set
            include_deleted: If True, include soft-deleted skills
            source: If set, filter to this exact source value

        Returns:
            List of matching Skills
        """
        query = "SELECT * FROM skills WHERE 1=1"
        params: list[Any] = []

        if not include_deleted:
            query += " AND deleted_at IS NULL"

        if source is not None:
            query += " AND source = ?"
            params.append(source)

        if project_id:
            if include_global:
                query += " AND (project_id = ? OR project_id IS NULL)"
                params.append(project_id)
            else:
                query += " AND project_id = ?"
                params.append(project_id)
        else:
            query += " AND project_id IS NULL"

        if enabled is not None:
            query += " AND enabled = ?"
            params.append(enabled)

        # Filter by category using JSON extraction in SQL to avoid under-filled results
        # Check both top-level $.category and nested $.skillport.category
        if category:
            query += """ AND (
                json_extract(metadata, '$.category') = ?
                OR json_extract(metadata, '$.skillport.category') = ?
            )"""
            params.extend([category, category])

        query += " ORDER BY name ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._fetchall(query, tuple(params))
        skills = [Skill.from_row(row) for row in rows]

        # Deduplicate when include_global is True: a skill may exist both
        # globally (project_id IS NULL) and per-project.  Prefer the
        # project-scoped version when both are present.
        if project_id and include_global:
            seen: dict[str, Skill] = {}
            for skill in skills:
                if skill.name not in seen or skill.project_id is not None:
                    seen[skill.name] = skill
            skills = list(seen.values())

        return skills

    def search_skills(
        self,
        query_text: str,
        project_id: str | None = None,
        limit: int = 20,
        include_deleted: bool = False,
    ) -> list[Skill]:
        """Search skills by name and description.

        This is a simple text search. For advanced search with keyword (FTS5)
        and embeddings, use SkillSearch from the skills module.

        Args:
            query_text: Text to search for
            project_id: Optional project scope
            limit: Maximum number of results
            include_deleted: If True, include soft-deleted skills

        Returns:
            List of matching Skills
        """
        # Escape LIKE wildcards
        escaped_query = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = """
            SELECT * FROM skills
            WHERE (name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')
        """
        params: list[Any] = [f"%{escaped_query}%", f"%{escaped_query}%"]

        if not include_deleted:
            sql += " AND deleted_at IS NULL"

        if project_id:
            sql += " AND (project_id = ? OR project_id IS NULL)"
            params.append(project_id)

        sql += " ORDER BY name ASC LIMIT ?"
        params.append(limit)

        rows = self._fetchall(sql, tuple(params))
        return [Skill.from_row(row) for row in rows]

    def list_core_skills(self, project_id: str | None = None) -> list[Skill]:
        """List skills with always_apply=true (efficiently via column query).

        Excludes soft-deleted and template skills.

        Args:
            project_id: Optional project scope

        Returns:
            List of core skills (always-apply skills)
        """
        query = "SELECT * FROM skills WHERE always_apply = 1 AND enabled = 1 AND deleted_at IS NULL"
        params: list[Any] = []

        if project_id:
            query += " AND (project_id = ? OR project_id IS NULL)"
            params.append(project_id)
        else:
            query += " AND project_id IS NULL"

        query += " ORDER BY name ASC"

        rows = self._fetchall(query, tuple(params))
        return [Skill.from_row(row) for row in rows]

    def skill_exists(self, skill_id: str, include_deleted: bool = False) -> bool:
        """Check if a skill with the given ID exists.

        Args:
            skill_id: The skill ID to check
            include_deleted: If True, include soft-deleted skills

        Returns:
            True if exists, False otherwise
        """
        if include_deleted:
            row = self._fetchone("SELECT 1 FROM skills WHERE id = ?", (skill_id,))
        else:
            row = self._fetchone(
                "SELECT 1 FROM skills WHERE id = ? AND deleted_at IS NULL", (skill_id,)
            )
        return row is not None

    def count_skills(
        self,
        project_id: str | None = None,
        enabled: bool | None = None,
        include_deleted: bool = False,
        source: str | None = None,
        include_global: bool = True,
    ) -> int:
        """Count skills matching criteria.

        Args:
            project_id: Filter by project
            enabled: Filter by enabled state
            include_deleted: If True, include soft-deleted skills
            source: If set, filter to this exact source value

        Returns:
            Number of matching skills
        """
        query = "SELECT COUNT(*) as count FROM skills WHERE 1=1"
        params: list[Any] = []

        if not include_deleted:
            query += " AND deleted_at IS NULL"

        if source is not None:
            query += " AND source = ?"
            params.append(source)

        if project_id:
            if include_global:
                query += " AND (project_id = ? OR project_id IS NULL)"
                params.append(project_id)
            else:
                query += " AND project_id = ?"
                params.append(project_id)
        else:
            query += " AND project_id IS NULL"

        if enabled is not None:
            query += " AND enabled = ?"
            params.append(enabled)

        row = self._fetchone(query, tuple(params))
        return row["count"] if row else 0
