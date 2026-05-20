"""Skill file I/O operations (read, write, delete, restore)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.storage.skills._models import SkillFile
from gobby.utils.id import generate_prefixed_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


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
        now = datetime.now(UTC).isoformat()
        changed = 0

        with self.db.transaction() as conn:
            # Get existing files for this skill (including soft-deleted)
            existing_rows = conn.execute(
                "SELECT id, path, content_hash, deleted_at FROM skill_files WHERE skill_id = ?",
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
                        # Restore and update
                        conn.execute(
                            """UPDATE skill_files
                               SET content = ?, content_hash = ?, size_bytes = ?,
                                   file_type = ?, deleted_at = NULL, updated_at = ?
                               WHERE id = ?""",
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
                        # Content changed — update
                        conn.execute(
                            """UPDATE skill_files
                               SET content = ?, content_hash = ?, size_bytes = ?,
                                   file_type = ?, updated_at = ?
                               WHERE id = ?""",
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
                    # else: hash matches, skip
                else:
                    # New file — insert
                    file_id = generate_prefixed_id("skf", f"{skill_id}:{f.path}")
                    conn.execute(
                        """INSERT INTO skill_files
                           (id, skill_id, path, file_type, content, content_hash,
                            size_bytes, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            file_id,
                            skill_id,
                            f.path,
                            f.file_type,
                            f.content,
                            f.content_hash,
                            f.size_bytes,
                            now,
                            now,
                        ),
                    )
                    changed += 1

            # Soft-delete orphan paths (files removed from disk)
            for path, info in existing_by_path.items():
                if path not in incoming_paths and not info["deleted"]:
                    conn.execute(
                        "UPDATE skill_files SET deleted_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, info["id"]),
                    )

        return changed

    def get_skill_files(
        self,
        skill_id: str,
        file_type: str | None = None,
        include_content: bool = False,
        exclude_license: bool = True,
    ) -> list[SkillFile]:
        """List files for a skill.

        Args:
            skill_id: Parent skill ID
            file_type: Optional filter by file type
            include_content: If True, include file content (default False for token efficiency)
            exclude_license: If True, exclude license files from results (default True)

        Returns:
            List of SkillFile objects (content field empty unless include_content=True)
        """
        conditions = ["skill_id = ?", "deleted_at IS NULL"]
        params: list[Any] = [skill_id]

        if file_type:
            conditions.append("file_type = ?")
            params.append(file_type)

        if exclude_license:
            conditions.append("file_type != 'license'")

        where = " AND ".join(conditions)
        cols = (
            "*"
            if include_content
            else "id, skill_id, path, file_type, content_hash, size_bytes, deleted_at, created_at, updated_at"
        )

        rows = self.db.fetchall(
            f"SELECT {cols} FROM skill_files WHERE {where} ORDER BY path",  # nosec B608
            tuple(params),
        )

        result = []
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

    def get_skill_file(self, skill_id: str, path: str) -> SkillFile | None:
        """Get a single skill file with content.

        Args:
            skill_id: Parent skill ID
            path: Relative file path

        Returns:
            SkillFile with content, or None if not found
        """
        row = self.db.fetchone(
            "SELECT * FROM skill_files WHERE skill_id = ? AND path = ? AND deleted_at IS NULL",
            (skill_id, path),
        )
        return SkillFile.from_row(row) if row else None

    def delete_skill_files(self, skill_id: str) -> int:
        """Soft-delete all files for a skill.

        Args:
            skill_id: Parent skill ID

        Returns:
            Number of files soft-deleted
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE skill_files SET deleted_at = ?, updated_at = ? "
                "WHERE skill_id = ? AND deleted_at IS NULL",
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
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE skill_files SET deleted_at = NULL, updated_at = ? "
                "WHERE skill_id = ? AND deleted_at IS NOT NULL",
                (now, skill_id),
            )
            return cursor.rowcount
