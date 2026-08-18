"""Skill file I/O operations (read, write, delete, restore)."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from gobby.storage.skills._models import Skill, SkillFile
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase, Transaction

logger = logging.getLogger(__name__)

# Deterministic id namespace: same (skill_id, path) -> same id, backing the
# UNIQUE(skill_id, path) constraint with id-level stability.
_NS_SKILL_FILES = uuid.uuid5(uuid.NAMESPACE_URL, "gobby:skill_files")
MAX_SKILL_FILE_PATH_BYTES = 1024


def _escape_like_prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_file_path(path: str) -> None:
    if len(path.encode("utf-8")) > MAX_SKILL_FILE_PATH_BYTES:
        raise ValueError(
            f"Skill file path exceeds {MAX_SKILL_FILE_PATH_BYTES} UTF-8 bytes: {path!r}"
        )


def _decode_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class SkillFilesMixin:
    """Mixin providing skill file I/O operations.

    Requires ``self.db`` (HubDatabase).
    """

    db: HubDatabase

    def set_skill_files(self, skill_id: str, files: list[SkillFile]) -> int:
        """Bulk upsert skill files in one transaction.

        Skips files where hash matches, updates changed files,
        soft-deletes orphan paths not in the incoming list.

        Args:
            skill_id: Parent skill ID
            files: List of SkillFile objects to upsert

        Returns:
            Number of files created or updated
        """
        with self.db.transaction() as conn:
            return self._set_skill_files(conn, skill_id, files)

    def _set_skill_files(self, conn: Transaction, skill_id: str, files: list[SkillFile]) -> int:
        """Synchronize skill files using an existing transaction."""
        for skill_file in files:
            _validate_file_path(skill_file.path)

        now = utc_now()
        changed = 0

        # Get existing files for this skill (including soft-deleted)
        existing_rows = conn.execute(
            "SELECT id, path, content_hash, deleted_at FROM skill_files WHERE skill_id = %s",
            (skill_id,),
        ).fetchall()
        existing_by_path: dict[str, dict[str, Any]] = {
            row["path"]: {
                "id": row["id"],
                "hash": row["content_hash"],
                "deleted": row["deleted_at"],
            }
            for row in existing_rows
        }

        incoming_paths: set[str] = set()

        for f in files:
            incoming_paths.add(f.path)
            existing = existing_by_path.get(f.path)

            if existing:
                if existing["deleted"]:
                    conn.execute(
                        """UPDATE skill_files
                           SET content = %s, content_hash = %s, size_bytes = %s,
                               file_type = %s, deleted_at = NULL, updated_at = %s
                           WHERE id = %s""",
                        (
                            f.content,
                            f.content_hash,
                            f.size_bytes,
                            f.file_type,
                            now,
                            existing["id"],
                        ),
                    )
                    changed += 1
                elif existing["hash"] != f.content_hash:
                    conn.execute(
                        """UPDATE skill_files
                           SET content = %s, content_hash = %s, size_bytes = %s,
                               file_type = %s, updated_at = %s
                           WHERE id = %s""",
                        (
                            f.content,
                            f.content_hash,
                            f.size_bytes,
                            f.file_type,
                            now,
                            existing["id"],
                        ),
                    )
                    changed += 1
            else:
                file_id = str(uuid.uuid5(_NS_SKILL_FILES, f"{skill_id}:{f.path}"))
                conn.execute(
                    """INSERT INTO skill_files
                       (id, skill_id, path, file_type, content, content_hash,
                        size_bytes)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (skill_id, path) DO UPDATE SET
                           content = excluded.content,
                           content_hash = excluded.content_hash,
                           size_bytes = excluded.size_bytes,
                           file_type = excluded.file_type,
                           deleted_at = NULL,
                           updated_at = excluded.updated_at""",
                    (
                        file_id,
                        skill_id,
                        f.path,
                        f.file_type,
                        f.content,
                        f.content_hash,
                        f.size_bytes,
                    ),
                )
                changed += 1

        # Soft-delete orphan paths (files removed from disk)
        for path, info in existing_by_path.items():
            if path not in incoming_paths and not info["deleted"]:
                conn.execute(
                    "UPDATE skill_files SET deleted_at = %s, updated_at = %s WHERE id = %s",
                    (now, now, info["id"]),
                )

        return changed

    def get_skill_files(
        self,
        skill_id: str,
        file_type: str | None = None,
        include_content: bool = False,
        exclude_license: bool = True,
        path_prefix: str | None = None,
        exclude_file_type: str | None = None,
        after_path: str | None = None,
        limit: int | None = None,
    ) -> list[SkillFile]:
        """List eligible files for a skill using ordered server-side filters."""
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        cte, params = self._filtered_files_cte(
            skill_id=skill_id,
            file_type=file_type,
            exclude_file_type=exclude_file_type,
            exclude_license=exclude_license,
            path_prefix=path_prefix,
            after_path=after_path,
        )
        cols = (
            "*"
            if include_content
            else "id, skill_id, path, file_type, content_hash, size_bytes, deleted_at, created_at, updated_at"
        )
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT %s"
            params.append(limit)
        rows = self.db.fetchall(
            f"WITH {cte} SELECT {cols} FROM eligible_files ORDER BY path{limit_sql}",  # nosec B608
            tuple(params),
        )
        result: list[SkillFile] = []
        for row in rows:
            if include_content:
                result.append(SkillFile.from_row(row))
            else:
                result.append(
                    SkillFile(
                        id=row["id"],
                        skill_id=row["skill_id"],
                        path=row["path"],
                        file_type=row["file_type"],
                        content="",  # Not loaded
                        content_hash=row["content_hash"],
                        size_bytes=row["size_bytes"],
                        deleted_at=row["deleted_at"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
        return result

    @staticmethod
    def _filtered_files_cte(
        *,
        skill_id: str | None = None,
        resolved_skill: bool = False,
        cte_prefix: str = "",
        path_prefix: str | None = None,
        file_type: str | None = None,
        exclude_file_type: str | None = None,
        exclude_license: bool = True,
        after_path: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Build the shared filtered and retrievable file sets."""
        if resolved_skill:
            source = "skill_files sf JOIN resolved_skill rs ON rs.id = sf.skill_id"
            conditions = ["sf.deleted_at IS NULL"]
            params: list[Any] = []
        else:
            if skill_id is None:
                raise ValueError("skill_id is required")
            source = "skill_files sf"
            conditions = ["sf.skill_id = %s", "sf.deleted_at IS NULL"]
            params = [skill_id]
        if path_prefix is not None:
            conditions.append("sf.path LIKE %s ESCAPE '\\'")
            params.append(f"{_escape_like_prefix(path_prefix)}%")
        if file_type is not None:
            conditions.append("sf.file_type = %s")
            params.append(file_type)
        if exclude_file_type is not None:
            conditions.append("sf.file_type != %s")
            params.append(exclude_file_type)
        if exclude_license:
            conditions.append("sf.file_type != 'license'")

        filtered = f"{cte_prefix}filtered_files"
        eligible = f"{cte_prefix}eligible_files"
        after_sql = ""
        if after_path is not None:
            after_sql = " AND path > %s"
        cte = (
            f"{filtered} AS (SELECT sf.* FROM {source} WHERE {' AND '.join(conditions)}), "
            f"{eligible} AS (SELECT * FROM {filtered} "
            f"WHERE octet_length(path) <= %s{after_sql})"
        )
        params.append(MAX_SKILL_FILE_PATH_BYTES)
        if after_path is not None:
            params.append(after_path)
        return cte, params

    @staticmethod
    def _resolved_skill_cte(
        *, skill_id: str | None, name: str | None, project_id: str | None
    ) -> tuple[str, list[Any]]:
        """Build the current ID-first, project-aware live-skill resolver."""
        cte = """resolved_skill AS (
            SELECT ranked.* FROM (
                SELECT s.*, 0 AS selector_rank
                FROM skills s
                WHERE %s::text IS NOT NULL
                  AND s.id::text = %s
                  AND s.deleted_at IS NULL
                UNION ALL
                SELECT s.*,
                    CASE WHEN %s::text IS NOT NULL AND s.project_id::text = %s
                        THEN 1 ELSE 2 END AS selector_rank
                FROM skills s
                WHERE %s::text IS NOT NULL
                  AND s.name = %s
                  AND s.deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM skills selected_by_id
                      WHERE %s::text IS NOT NULL
                        AND selected_by_id.id::text = %s
                        AND selected_by_id.deleted_at IS NULL
                  )
                  AND (
                      (
                          %s::text IS NOT NULL
                          AND s.project_id::text = %s
                          AND ('/' || translate(coalesce(s.source_path, ''), chr(92), '/') || '/')
                              NOT LIKE '%%/gobby/install/shared/skills/%%'
                      )
                      OR s.project_id IS NULL
                  )
            ) ranked
            ORDER BY selector_rank
            LIMIT 1
        )"""
        return cte, [
            skill_id,
            skill_id,
            project_id,
            project_id,
            name,
            name,
            skill_id,
            skill_id,
            project_id,
            project_id,
        ]

    def get_skill_file_page(
        self,
        skill_id: str | None,
        *,
        name: str | None = None,
        project_id: str | None = None,
        path_prefix: str | None = None,
        file_type: str | None = None,
        exclude_file_type: str | None = None,
        exclude_license: bool = True,
        after_path: str | None = None,
        limit: int,
    ) -> dict[str, Any] | None:
        """Resolve a skill and return one bounded file page and its counts."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        resolver, params = self._resolved_skill_cte(
            skill_id=skill_id, name=name, project_id=project_id
        )
        cte, file_params = self._filtered_files_cte(
            resolved_skill=True,
            path_prefix=path_prefix,
            file_type=file_type,
            exclude_file_type=exclude_file_type,
            exclude_license=exclude_license,
            after_path=after_path,
        )
        params.extend(file_params)
        params.append(limit)
        row = self.db.fetchone(
            f"""WITH {resolver},
                {cte},
                page AS (
                    SELECT path, file_type, size_bytes
                    FROM eligible_files ORDER BY path LIMIT %s
                )
                SELECT
                    COALESCE(
                        (SELECT jsonb_agg(
                            jsonb_build_object(
                                'path', path,
                                'file_type', file_type,
                                'size_bytes', size_bytes
                            ) ORDER BY path
                        ) FROM page),
                        '[]'::jsonb
                    ) AS files,
                    (SELECT count(*) FROM eligible_files) AS total_files,
                    (SELECT count(*) FROM eligible_files) -
                        (SELECT count(*) FROM page) AS remaining_file_count,
                    (SELECT count(*) FROM filtered_files
                        WHERE octet_length(path) > {MAX_SKILL_FILE_PATH_BYTES}
                    ) AS omitted_oversized_path_count,
                    resolved_skill.id AS resolved_skill_id,
                    resolved_skill.name AS resolved_skill_name
                FROM resolved_skill""",  # nosec B608
            tuple(params),
        )
        if row is None:
            return None
        return {
            "skill_id": str(row["resolved_skill_id"]),
            "name": row["resolved_skill_name"],
            "files": list(_decode_json(row["files"]) or []),
            "total_files": int(row["total_files"]),
            "remaining_file_count": int(row["remaining_file_count"]),
            "omitted_oversized_path_count": int(row["omitted_oversized_path_count"]),
        }

    def get_skill_with_manifest(
        self,
        *,
        skill_id: str | None,
        name: str | None,
        project_id: str | None,
        file_limit: int,
        directory_limit: int,
        exclude_license: bool = True,
    ) -> dict[str, Any] | None:
        """Resolve a skill row and its bounded manifest in one statement."""
        if file_limit < 1 or directory_limit < 1:
            raise ValueError("manifest limits must be at least 1")
        resolver, params = self._resolved_skill_cte(
            skill_id=skill_id, name=name, project_id=project_id
        )
        manifest_cte, manifest_params = self._filtered_files_cte(
            resolved_skill=True,
            cte_prefix="manifest_",
            exclude_file_type="script",
            exclude_license=exclude_license,
        )
        scripts_cte, scripts_params = self._filtered_files_cte(
            resolved_skill=True,
            cte_prefix="script_",
            file_type="script",
            exclude_license=exclude_license,
        )
        params.extend(manifest_params)
        params.extend(scripts_params)
        params.extend([file_limit, directory_limit])
        row = self.db.fetchone(
            f"""WITH {resolver},
                {manifest_cte},
                {scripts_cte},
                manifest_page AS (
                    SELECT path, file_type, size_bytes
                    FROM manifest_eligible_files ORDER BY path LIMIT %s
                ),
                script_directories AS (
                    SELECT
                        split_part(substr(path, length('scripts/') + 1), '/', 1) AS name,
                        count(*) AS file_count
                    FROM script_eligible_files
                    GROUP BY 1
                ),
                script_directory_page AS (
                    SELECT name, file_count
                    FROM script_directories ORDER BY name LIMIT %s
                )
                SELECT resolved_skill.*,
                    COALESCE(
                        (SELECT jsonb_agg(
                            jsonb_build_object(
                                'path', path,
                                'file_type', file_type,
                                'size_bytes', size_bytes
                            ) ORDER BY path
                        ) FROM manifest_page),
                        '[]'::jsonb
                    ) AS manifest_files,
                    (SELECT count(*) FROM manifest_eligible_files) AS manifest_total_files,
                    (SELECT count(*) FROM manifest_eligible_files) -
                        (SELECT count(*) FROM manifest_page) AS manifest_remaining_file_count,
                    (SELECT count(*) FROM manifest_filtered_files
                        WHERE octet_length(path) > {MAX_SKILL_FILE_PATH_BYTES}
                    ) +
                    (SELECT count(*) FROM script_filtered_files
                        WHERE octet_length(path) > {MAX_SKILL_FILE_PATH_BYTES}
                    ) AS omitted_oversized_path_count,
                    (SELECT count(*) FROM script_eligible_files) AS script_total_files,
                    COALESCE((SELECT sum(size_bytes) FROM script_eligible_files), 0)
                        AS script_total_bytes,
                    COALESCE(
                        (SELECT jsonb_object_agg(name, file_count ORDER BY name)
                         FROM script_directory_page),
                        '{{}}'::jsonb
                    ) AS script_directories,
                    (SELECT count(*) FROM script_directories) -
                        (SELECT count(*) FROM script_directory_page)
                        AS script_remaining_directory_count,
                    (SELECT count(*) FROM script_eligible_files) -
                        COALESCE((SELECT sum(file_count) FROM script_directory_page), 0)
                        AS script_remaining_file_count
                FROM resolved_skill""",  # nosec B608
            tuple(params),
        )
        if row is None:
            return None
        skill = Skill.from_row(row)
        return {
            "skill": skill,
            "files": list(_decode_json(row["manifest_files"]) or []),
            "total_files": int(row["manifest_total_files"]),
            "remaining_file_count": int(row["manifest_remaining_file_count"]),
            "omitted_oversized_path_count": int(row["omitted_oversized_path_count"]),
            "scripts": {
                "total_files": int(row["script_total_files"]),
                "total_bytes": int(row["script_total_bytes"]),
                "per_top_level_dir": dict(_decode_json(row["script_directories"]) or {}),
                "remaining_directory_count": int(row["script_remaining_directory_count"]),
                "remaining_file_count": int(row["script_remaining_file_count"]),
            },
        }

    def get_skill_with_scripts(
        self,
        *,
        name: str,
        project_id: str | None,
    ) -> dict[str, Any] | None:
        """Resolve a skill and its complete scripts inventory in one statement."""
        resolver, params = self._resolved_skill_cte(
            skill_id=None,
            name=name,
            project_id=project_id,
        )
        row = self.db.fetchone(
            f"""WITH {resolver}
                SELECT resolved_skill.*,
                    COALESCE(
                        (
                            SELECT jsonb_agg(
                                jsonb_build_object(
                                    'path', files.path,
                                    'content', files.content,
                                    'content_hash', files.content_hash,
                                    'size_bytes', files.size_bytes
                                ) ORDER BY files.path
                            )
                            FROM skill_files files
                            WHERE files.skill_id = resolved_skill.id
                              AND files.deleted_at IS NULL
                              AND files.file_type = 'script'
                              AND files.path LIKE 'scripts/%%'
                        ),
                        '[]'::jsonb
                    ) AS script_files
                FROM resolved_skill""",  # nosec B608
            tuple(params),
        )
        if row is None:
            return None
        files = list(_decode_json(row["script_files"]) or [])
        return {"skill": Skill.from_row(row), "files": files}

    def get_skill_file(self, skill_id: str, path: str) -> SkillFile | None:
        """Get a single skill file with content.

        Args:
            skill_id: Parent skill ID
            path: Relative file path

        Returns:
            SkillFile with content, or None if not found
        """
        row = self.db.fetchone(
            "SELECT * FROM skill_files WHERE skill_id = %s AND path = %s AND deleted_at IS NULL",
            (skill_id, path),
        )
        return SkillFile.from_row(row) if row else None

    def update_skill_file(self, skill_id: str, path: str, content: str) -> SkillFile | None:
        """Update one skill file's content in place.

        Args:
            skill_id: Parent skill ID
            path: Relative file path
            content: New file text content

        Returns:
            The updated SkillFile with content, or None if no live file matches
        """
        content_bytes = content.encode("utf-8")
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE skill_files
                   SET content = %s, content_hash = %s, size_bytes = %s, updated_at = %s
                   WHERE skill_id = %s AND path = %s AND deleted_at IS NULL""",
                (content, content_hash, len(content_bytes), utc_now(), skill_id, path),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_skill_file(skill_id, path)

    def delete_skill_files(self, skill_id: str) -> int:
        """Soft-delete all files for a skill.

        Args:
            skill_id: Parent skill ID

        Returns:
            Number of files soft-deleted
        """
        now = utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE skill_files SET deleted_at = %s, updated_at = %s "
                "WHERE skill_id = %s AND deleted_at IS NULL",
                (now, now, skill_id),
            )
            return cursor.rowcount

    def restore_skill_files(self, skill_id: str) -> int:
        """Restore soft-deleted files for a skill.

        Args:
            skill_id: Parent skill ID

        Returns:
            Number of files restored
        """
        with self.db.transaction() as conn:
            return self._restore_skill_files(conn, skill_id)

    def _restore_skill_files(self, conn: Transaction, skill_id: str) -> int:
        """Restore soft-deleted files using an existing transaction."""
        cursor = conn.execute(
            "UPDATE skill_files SET deleted_at = NULL, updated_at = %s "
            "WHERE skill_id = %s AND deleted_at IS NOT NULL",
            (utc_now(), skill_id),
        )
        return cursor.rowcount
