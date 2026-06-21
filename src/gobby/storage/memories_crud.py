import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from gobby.storage.memories_base import MemoryStoreBase
from gobby.storage.memories_models import (
    MEMORY_UUID_NAMESPACE,
    Memory,
    Visibility,
    visibility_predicate,
)
from gobby.storage.sql_dialect import newer_than_now_expr

logger = logging.getLogger(__name__)


class MemoryCrudMixin(MemoryStoreBase):
    def create_memory(
        self,
        content: str,
        memory_type: str = "fact",
        project_id: str | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        # Validate that content is not empty
        if not content or not content.strip():
            logger.warning("Skipping memory creation: empty content provided")
            raise ValueError("Memory content cannot be empty")

        now = datetime.now(UTC).isoformat()
        # Normalize content for consistent ID generation (avoid duplicates from
        # whitespace differences)
        normalized_content = content.strip()
        legacy_memory_id = str(uuid.uuid5(MEMORY_UUID_NAMESPACE, normalized_content))
        current_memory_id_seed = json.dumps(
            {"content": normalized_content, "project_id": project_id},
            sort_keys=True,
            separators=(",", ":"),
        )
        current_memory_id = str(uuid.uuid5(MEMORY_UUID_NAMESPACE, current_memory_id_seed))
        memory_id = current_memory_id

        tags_json = json.dumps(tags) if tags else None

        changed = False
        row: Any | None = None
        with self.db.transaction() as conn:
            # source_id proximity dedup: if the same session created a very similar
            # memory within the last 60 seconds, treat it as a duplicate.
            if source_session_id:
                recent_cutoff_sql = newer_than_now_expr(self.db, "created_at", "%s", "second")
                recent = conn.execute(
                    f"""SELECT * FROM memories
                       WHERE source_session_id = %s
                         AND project_id IS NOT DISTINCT FROM %s
                         AND deleted_at IS NULL
                         AND {recent_cutoff_sql}
                       ORDER BY created_at DESC, id DESC LIMIT 1""",
                    (source_session_id, project_id, 60),
                ).fetchone()
                if recent and normalized_content == str(recent["content"]).strip():
                    return Memory.from_row(recent)

            if project_id is None:
                memory_id = legacy_memory_id
            else:
                legacy_row = conn.execute(
                    "SELECT 1 FROM memories WHERE id = %s",
                    (legacy_memory_id,),
                ).fetchone()
                if legacy_row is not None:
                    memory_id = legacy_memory_id

            existing_row = conn.execute(
                "SELECT deleted_at FROM memories WHERE id = %s",
                (memory_id,),
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT INTO memories (
                    id, project_id, memory_type, content, source_type,
                    source_session_id, access_count, tags,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    deleted_at = NULL,
                    dream_action = NULL,
                    last_dreamed_at = NULL,
                    updated_at = CASE
                        WHEN memories.deleted_at IS NOT NULL THEN excluded.updated_at
                        ELSE memories.updated_at
                    END
                RETURNING *
                """,
                (
                    memory_id,
                    project_id,
                    memory_type,
                    normalized_content,
                    source_type,
                    source_session_id,
                    tags_json,
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
            changed = existing_row is None or existing_row["deleted_at"] is not None

        if row is None:
            raise RuntimeError(f"Memory {memory_id} not found after creation")

        if changed:
            self._notify_listeners()
        return Memory.from_row(row)

    def get_memory(
        self,
        memory_id: str,
        project_id: str | None = None,
        *,
        visibility: Visibility = "active",
    ) -> Memory:
        """Get a memory by ID, optionally scoped to a project.

        When project_id is provided, only returns the memory if it belongs to
        that project or is a global memory (project_id IS NULL). This prevents
        cross-project memory leakage.

        Args:
            memory_id: The memory UUID to look up
            project_id: If provided, enforce project scoping

        Raises:
            ValueError: If memory not found or not accessible in the given project
        """
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        if project_id:
            row = self.db.fetchone(
                "SELECT * FROM memories WHERE id = %s "
                f"AND (project_id = %s OR project_id IS NULL){vis_clause}",
                (memory_id, project_id),
            )
        else:
            row = self.db.fetchone(
                f"SELECT * FROM memories WHERE id = %s{vis_clause}", (memory_id,)
            )
        if not row:
            raise ValueError(f"Memory {memory_id} not found")
        return Memory.from_row(row)

    def get_memories(
        self,
        memory_ids: list[str],
        project_id: str | None = None,
        *,
        visibility: Visibility = "active",
    ) -> list[Memory]:
        """Return multiple memories, preserving the requested order."""
        if not memory_ids:
            return []

        placeholders = ", ".join("%s" for _ in memory_ids)
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        if project_id:
            rows = self.db.fetchall(
                f"SELECT * FROM memories WHERE id IN ({placeholders}) "
                f"AND (project_id = %s OR project_id IS NULL){vis_clause}",
                (*memory_ids, project_id),
            )
        else:
            rows = self.db.fetchall(
                f"SELECT * FROM memories WHERE id IN ({placeholders}){vis_clause}",
                tuple(memory_ids),
            )

        memories_by_id = {row["id"]: Memory.from_row(row) for row in rows}
        return [
            memories_by_id[memory_id] for memory_id in memory_ids if memory_id in memories_by_id
        ]

    def memory_exists(self, memory_id: str) -> bool:
        """Check if a memory with the given ID exists."""
        row = self.db.fetchone("SELECT 1 FROM memories WHERE id = %s", (memory_id,))
        return row is not None

    def content_exists(
        self, content: str, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> bool:
        """Check if a memory with identical content already exists.

        Scopes duplicate detection to the exact project, treating ``NULL`` as
        the global scope.

        Args:
            content: The content to check for
            project_id: Project scope to check. ``None`` checks global memories.

        Returns:
            True if a memory with identical content exists
        """
        normalized_content = content.strip()
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        row = self.db.fetchone(
            f"""
            SELECT 1 FROM memories
             WHERE content = %s
               AND project_id IS NOT DISTINCT FROM %s{vis_clause}
             ORDER BY created_at ASC, id ASC
             LIMIT 1
            """,
            (normalized_content, project_id),
        )
        return row is not None

    def get_memory_by_content(
        self, content: str, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> Memory | None:
        """Get a memory by its exact content.

        Uses project-scoped lookup, matching the behavior of content_exists().

        Args:
            content: The exact content to look up (will be normalized)
            project_id: Project scope to check. ``None`` checks global memories.

        Returns:
            The Memory object if found, None otherwise
        """
        normalized_content = content.strip()
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        row = self.db.fetchone(
            f"""
            SELECT * FROM memories
             WHERE content = %s
               AND project_id IS NOT DISTINCT FROM %s{vis_clause}
             ORDER BY created_at ASC, id ASC
             LIMIT 1
            """,
            (normalized_content, project_id),
        )
        if row:
            return Memory.from_row(row)
        return None

    def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        updates = []
        params: list[Any] = []

        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("Memory content cannot be empty")
            raise ValueError("Memory content cannot be updated; create a new memory instead")
        if tags is not None:
            updates.append("tags = %s")
            params.append(json.dumps(tags))

        if not updates:
            return self.get_memory(memory_id)

        updates.append("updated_at = %s")
        params.append(datetime.now(UTC).isoformat())
        params.append(memory_id)

        sql = f"UPDATE memories SET {', '.join(updates)} WHERE id = %s"  # nosec B608

        with self.db.transaction() as conn:
            cursor = conn.execute(sql, tuple(params))
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")

        self._notify_listeners()
        return self.get_memory(memory_id)

    def update_memory_project(self, memory_id: str, project_id: str) -> Memory:
        """Update a memory's project assignment and notify storage listeners."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET project_id = %s, updated_at = %s WHERE id = %s",
                (project_id, datetime.now(UTC).isoformat(), memory_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")

        self._notify_listeners()
        return self.get_memory(memory_id)

    def rescope_memory(self, memory_id: str, new_project_id: str | None) -> Memory:
        """Update a memory's project scope without changing content recency."""
        with self.db.transaction() as conn:
            if new_project_id is not None:
                project_row = conn.execute(
                    "SELECT 1 FROM projects WHERE id = %s",
                    (new_project_id,),
                ).fetchone()
                if project_row is None:
                    raise ValueError(f"Project {new_project_id} not found")
            cursor = conn.execute(
                "UPDATE memories SET project_id = %s WHERE id = %s",
                (new_project_id, memory_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")

        self._notify_listeners()
        return self.get_memory(memory_id, visibility="all")

    def delete_memory(self, memory_id: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
            if cursor.rowcount == 0:
                return False
        self._notify_listeners()
        return True
