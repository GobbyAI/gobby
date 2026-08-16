"""Skill metadata CRUD operations (create, get, list, update, delete)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from psycopg.errors import UniqueViolation

from gobby.storage.skills._bundled import (
    BUNDLED_TEMPLATE_PROJECT_SKILL_ERROR,
    is_bundled_template_path,
)
from gobby.storage.skills._errors import (
    DuplicateSkillError,
    SkillMetadataValidationError,
    SkillScopeConflictError,
)
from gobby.storage.skills._models import Skill, SkillSourceType, SkillUsageStats
from gobby.storage.sql_dialect import json_text_expr
from gobby.utils.datetime import utc_now

# Deterministic id namespace: same (name, project, source) -> same id. Skill id
# stability matters because skill_files ids are seeded with the skill id.
_NS_SKILLS = uuid.uuid5(uuid.NAMESPACE_URL, "gobby:skills")

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase, Transaction

logger = logging.getLogger(__name__)

_UNSET: Any = object()


class _SkillMetadataHost(Protocol):
    db: HubDatabase

    def _notify_change(
        self,
        event_type: str,
        skill_id: str,
        skill_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def delete_skill_files(self, skill_id: str) -> int: ...

    def restore_skill_files(self, skill_id: str) -> int: ...

    def _restore_skill_files(self, conn: Transaction, skill_id: str) -> int: ...


class SkillMetadataMixin:
    """Mixin providing skill metadata CRUD operations.

    Requires ``self.db`` (HubDatabase) and ``self._notify_change()``.
    """

    db: HubDatabase

    def _host(self) -> _SkillMetadataHost:
        return cast(_SkillMetadataHost, self)

    def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> Mapping[str, Any] | None:
        """Run a read query in a new transaction.

        Callers that already own a transaction should execute on that connection
        directly to avoid nested transaction behavior.
        """
        with self.db.transaction() as conn:
            row: Mapping[str, Any] | None = conn.execute(query, params).fetchone()
            return row

    def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[Mapping[str, Any]]:
        """Run a read query in a new transaction.

        Callers that already own a transaction should execute on that connection
        directly to avoid nested transaction behavior.
        """
        with self.db.transaction() as conn:
            rows: list[Mapping[str, Any]] = conn.execute(query, params).fetchall()
            return rows

    def _create_skill_in_transaction(
        self,
        conn: Transaction,
        *,
        name: str,
        description: str,
        content: str,
        version: str | None,
        license: str | None,
        compatibility: str | None,
        allowed_tools: list[str] | None,
        metadata: dict[str, Any] | None,
        source_path: str | None,
        source_type: SkillSourceType | None,
        source_ref: str | None,
        hub_name: str | None,
        hub_slug: str | None,
        hub_version: str | None,
        enabled: bool,
        always_apply: bool,
        injection_format: str,
        project_id: str | None,
        source: str,
    ) -> str:
        """Create a skill row using an existing publication transaction."""
        from gobby.skills.parser import SkillParseError, validate_runtime_metadata

        try:
            validate_runtime_metadata(metadata)
        except SkillParseError as exc:
            raise SkillMetadataValidationError(str(exc)) from exc

        if project_id is not None:
            source = "project"
            if is_bundled_template_path(source_path):
                raise ValueError(f"Skill '{name}': {BUNDLED_TEMPLATE_PROJECT_SKILL_ERROR}")

        skill_id = str(uuid.uuid5(_NS_SKILLS, f"{name}:{project_id or 'global'}:{source}"))
        if self.skill_exists(skill_id, include_deleted=True):
            skill_id = str(uuid.uuid4())
        now = utc_now()

        try:
            conn.execute(
                """
                INSERT INTO skills (
                    id, name, description, content, version, license,
                    compatibility, allowed_tools, metadata, source_path,
                    source_type, source_ref, hub_name, hub_slug, hub_version,
                    enabled, always_apply, injection_format, project_id,
                    source, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    skill_id,
                    name,
                    description,
                    content,
                    version,
                    license,
                    compatibility,
                    json.dumps(allowed_tools) if allowed_tools else None,
                    json.dumps(metadata) if metadata else None,
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
        except UniqueViolation as exc:
            raise DuplicateSkillError(
                f"Skill '{name}' (source={source}) already exists"
                + (f" in project {project_id}" if project_id else " globally")
            ) from exc
        return skill_id

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
        with self.db.transaction() as conn:
            skill_id = self._create_skill_in_transaction(
                conn,
                name=name,
                description=description,
                content=content,
                version=version,
                license=license,
                compatibility=compatibility,
                allowed_tools=allowed_tools,
                metadata=metadata,
                source_path=source_path,
                source_type=source_type,
                source_ref=source_ref,
                hub_name=hub_name,
                hub_slug=hub_slug,
                hub_version=hub_version,
                enabled=enabled,
                always_apply=always_apply,
                injection_format=injection_format,
                project_id=project_id,
                source=source,
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
            row = self._fetchone("SELECT * FROM skills WHERE id = %s", (skill_id,))
        else:
            row = self._fetchone(
                "SELECT * FROM skills WHERE id = %s AND deleted_at IS NULL",
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
        placeholders = ",".join("%s" for _ in skill_ids)
        rows = self._fetchall(
            # Placeholders are generated; values remain bound.
            f"SELECT * FROM skills WHERE id IN ({placeholders}) AND deleted_at IS NULL",  # nosec B608
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
        installed-copy precedence of typed definition managers. When an
        installed copy exists it shadows the template.

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
        conditions = ["name = %s"]
        params: list[Any] = [name]

        if not include_deleted:
            conditions.append("deleted_at IS NULL")

        if source is not None:
            conditions.append("source = %s")
            params.append(source)

        where = " AND ".join(conditions)

        if project_id:
            # First try project-scoped skill
            row = self._fetchone(
                f"SELECT * FROM skills WHERE {where} AND project_id = %s",  # nosec B608
                (*params, project_id),
            )
            # Live resolution never serves a project row sourced from a
            # bundled template tree: it is a stale shadow of the
            # bundled-synced installed row (#17606). Deleted-inclusive
            # queries are bookkeeping (dup checks, restore) and still see it.
            if (
                row is not None
                and not include_deleted
                and is_bundled_template_path(row.get("source_path"))
            ):
                logger.warning(
                    "Ignoring project-scoped skill '%s' sourced from bundled template path %s",
                    name,
                    row.get("source_path"),
                )
                row = None
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
            updates.append("name = %s")
            params.append(name)
        if description is not None:
            updates.append("description = %s")
            params.append(description)
        if content is not None:
            updates.append("content = %s")
            params.append(content)
        if version is not _UNSET:
            updates.append("version = %s")
            params.append(version)
        if license is not _UNSET:
            updates.append("license = %s")
            params.append(license)
        if compatibility is not _UNSET:
            updates.append("compatibility = %s")
            params.append(compatibility)
        if allowed_tools is not _UNSET:
            updates.append("allowed_tools = %s")
            params.append(json.dumps(allowed_tools) if allowed_tools else None)
        if metadata is not _UNSET:
            from gobby.skills.parser import SkillParseError, validate_runtime_metadata

            try:
                validate_runtime_metadata(metadata)
            except SkillParseError as exc:
                raise SkillMetadataValidationError(str(exc)) from exc
            updates.append("metadata = %s")
            params.append(json.dumps(metadata) if metadata else None)
        if source_path is not _UNSET:
            updates.append("source_path = %s")
            params.append(source_path)
        if source_type is not _UNSET:
            updates.append("source_type = %s")
            params.append(source_type)
        if source_ref is not _UNSET:
            updates.append("source_ref = %s")
            params.append(source_ref)
        if hub_name is not _UNSET:
            updates.append("hub_name = %s")
            params.append(hub_name)
        if hub_slug is not _UNSET:
            updates.append("hub_slug = %s")
            params.append(hub_slug)
        if hub_version is not _UNSET:
            updates.append("hub_version = %s")
            params.append(hub_version)
        if enabled is not None:
            updates.append("enabled = %s")
            params.append(enabled)
        if always_apply is not None:
            updates.append("always_apply = %s")
            params.append(always_apply)
        if injection_format is not None:
            updates.append("injection_format = %s")
            params.append(injection_format)
        if source is not None:
            updates.append("source = %s")
            params.append(source)
        if project_id is not _UNSET:
            updates.append("project_id = %s")
            params.append(project_id)

        if not updates:
            return self.get_skill(skill_id)

        updates.append("updated_at = %s")
        params.append(utc_now())
        params.append(skill_id)

        sql = f"UPDATE skills SET {', '.join(updates)} WHERE id = %s"  # nosec B608

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

        now = utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE skills SET deleted_at = %s, updated_at = %s "
                "WHERE id = %s AND deleted_at IS NULL",
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
            cursor = conn.execute("DELETE FROM skills WHERE id = %s", (skill_id,))
            if cursor.rowcount == 0:
                return False

        self._host()._notify_change("delete", skill_id, skill_name)
        return True

    def purge_soft_deleted_before(self, cutoff: datetime, *, limit: int = 500) -> int:
        """Permanently delete a bounded batch of skills deleted before ``cutoff``."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                WITH expired AS (
                    SELECT id
                    FROM skills
                    WHERE deleted_at IS NOT NULL AND deleted_at < %s
                    ORDER BY deleted_at
                    LIMIT %s
                )
                DELETE FROM skills
                WHERE id IN (SELECT id FROM expired)
                """,
                (cutoff, limit),
            )
            return cursor.rowcount

    def restore(self, skill_id: str) -> Skill:
        """Restore a soft-deleted skill.

        Args:
            skill_id: The skill ID to restore

        Returns:
            The restored Skill

        Raises:
            ValueError: If skill not found
        """
        now = utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE skills SET deleted_at = NULL, updated_at = %s WHERE id = %s",
                (now, skill_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Skill {skill_id} not found")
            self._host()._restore_skill_files(conn, skill_id)

        host = self._host()
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
            ValueError: If skill not found, or if the skill is sourced from a
                bundled template tree (project scope would shadow the
                bundled-synced installed row).
        """
        skill = self.get_skill(skill_id)
        if is_bundled_template_path(skill.source_path):
            raise ValueError(f"Skill '{skill.name}': {BUNDLED_TEMPLATE_PROJECT_SKILL_ERROR}")
        existing = self.get_by_name(
            skill.name,
            project_id=project_id,
            include_global=False,
            include_deleted=True,
            source="project",
        )
        if existing is not None and existing.id != skill.id:
            raise SkillScopeConflictError(
                f"Skill '{skill.name}' already exists in project {project_id}"
            )
        return self.update_skill(skill_id, source="project", project_id=project_id)

    def purge_bundled_template_project_skills(self) -> list[Skill]:
        """Soft-delete project-scoped rows sourced from bundled template trees.

        Such rows shadow bundled-synced installed rows with stale template
        content and should never exist (#17606); creation is blocked, but rows
        written before that guard (or by older daemons) are healed here on
        bundled sync.

        Returns:
            The skills that were soft-deleted.
        """
        rows = self._fetchall(
            "SELECT * FROM skills WHERE project_id IS NOT NULL "
            "AND deleted_at IS NULL AND source_path IS NOT NULL"
        )
        purged: list[Skill] = []
        for row in rows:
            skill = Skill.from_row(row)
            if not is_bundled_template_path(skill.source_path):
                continue
            if self.delete_skill(skill.id):
                logger.warning(
                    "Purged project-scoped skill '%s' (project %s) sourced from bundled template path %s",
                    skill.name,
                    skill.project_id,
                    skill.source_path,
                )
                purged.append(skill)
        return purged

    def move_to_installed(self, skill_id: str) -> Skill:
        """Move a project-scoped skill back to installed scope.

        Args:
            skill_id: The skill ID

        Returns:
            The updated Skill

        Raises:
            ValueError: If skill not found.
        """
        skill = self.get_skill(skill_id)
        existing = self.get_by_name(
            skill.name,
            project_id=None,
            include_deleted=True,
            source="installed",
        )
        if existing is not None and existing.id != skill.id:
            raise SkillScopeConflictError(f"Skill '{skill.name}' already exists globally")
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
            query += " AND source = %s"
            params.append(source)

        if project_id:
            if include_global:
                query += " AND (project_id = %s OR project_id IS NULL)"
                params.append(project_id)
            else:
                query += " AND project_id = %s"
                params.append(project_id)
        else:
            query += " AND project_id IS NULL"

        if enabled is not None:
            query += " AND enabled = %s"
            params.append(enabled)

        # Filter by category using JSON extraction in SQL to avoid under-filled results
        # Check both top-level $.category and nested $.skillport.category
        if category:
            category_sql = json_text_expr(self.db, "metadata", "category")
            skillport_category_sql = json_text_expr(self.db, "metadata", "skillport", "category")
            query += f""" AND (
                {category_sql} = %s
                OR {skillport_category_sql} = %s
            )"""  # nosec B608 # JSON expressions are generated from static keys.
            params.extend([category, category])

        if project_id and include_global:
            query = f"""
                SELECT * FROM (
                    SELECT DISTINCT ON (name) *
                    FROM ({query}) AS visible_skills
                    ORDER BY name, (project_id IS NOT NULL) DESC
                ) AS deduped_skills
                ORDER BY name ASC
            """  # nosec B608
        else:
            query += " ORDER BY name ASC"
        if limit >= 0:
            query += " LIMIT %s OFFSET %s"
            params.extend([limit, offset])
        elif offset > 0:
            query += " OFFSET %s"
            params.append(offset)

        rows = self._fetchall(query, tuple(params))
        return [Skill.from_row(row) for row in rows]

    def get_skill_usage_stats(self, skill_names: list[str]) -> dict[str, SkillUsageStats]:
        """Return session load count and latest use for each requested skill name."""
        names = tuple(dict.fromkeys(skill_names))
        if not names:
            return {}

        placeholders = ", ".join("%s" for _ in names)
        rows = self._fetchall(
            f"""
            SELECT skill_name, COUNT(*) AS loads, MAX(created_at) AS last_used
            FROM session_skills
            WHERE skill_name IN ({placeholders})
            GROUP BY skill_name
            """,  # nosec B608 # placeholders are generated; values remain bound.
            names,
        )
        return {
            str(row["skill_name"]): SkillUsageStats(
                loads=int(row["loads"]),
                last_used=cast(datetime, row["last_used"]),
            )
            for row in rows
        }

    def search_skills(
        self,
        query_text: str,
        project_id: str | None = None,
        limit: int = 20,
        include_deleted: bool = False,
    ) -> list[Skill]:
        """Search skills by name and description.

        This is a simple text search. For advanced keyword and embedding search,
        use SkillSearch from the skills module.

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
            WHERE (name LIKE %s ESCAPE '\\' OR description LIKE %s ESCAPE '\\')
        """
        params: list[Any] = [f"%{escaped_query}%", f"%{escaped_query}%"]

        if not include_deleted:
            sql += " AND deleted_at IS NULL"

        if project_id:
            sql += " AND (project_id = %s OR project_id IS NULL)"
            params.append(project_id)

        sql += " ORDER BY name ASC LIMIT %s"
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
        query = (
            "SELECT * FROM skills "
            "WHERE always_apply IS TRUE AND enabled IS TRUE AND deleted_at IS NULL"
        )
        params: list[Any] = []

        if project_id:
            query += " AND (project_id = %s OR project_id IS NULL)"
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
            row = self._fetchone("SELECT 1 FROM skills WHERE id = %s", (skill_id,))
        else:
            row = self._fetchone(
                "SELECT 1 FROM skills WHERE id = %s AND deleted_at IS NULL", (skill_id,)
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
        count_expression = "COUNT(DISTINCT name)" if project_id and include_global else "COUNT(*)"
        query = f"SELECT {count_expression} as count FROM skills WHERE 1=1"  # nosec B608
        params: list[Any] = []

        if not include_deleted:
            query += " AND deleted_at IS NULL"

        if source is not None:
            query += " AND source = %s"
            params.append(source)

        if project_id:
            if include_global:
                query += " AND (project_id = %s OR project_id IS NULL)"
                params.append(project_id)
            else:
                query += " AND project_id = %s"
                params.append(project_id)
        else:
            query += " AND project_id IS NULL"

        if enabled is not None:
            query += " AND enabled = %s"
            params.append(enabled)

        row = self._fetchone(query, tuple(params))
        return row["count"] if row else 0
