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
        # Global dedup: ID based on content only (project_id stored but not in ID)
        # This aligns with content_exists() which checks globally
        memory_id = str(uuid.uuid5(MEMORY_UUID_NAMESPACE, normalized_content))

        # Check if memory already exists to avoid duplicate insert errors
        existing_row = self.db.fetchone("SELECT * FROM memories WHERE id = %s", (memory_id,))
        if existing_row:
            # Deterministic uuid5 collision: identical content is already stored.
            # If dream GC soft-hid that row, reactivate it here — this is the one
            # create path every backend/storage caller funnels through, so a
            # hidden collision must restore rather than return an invisible row.
            if existing_row["deleted_at"] is not None:
                self.restore_memory(memory_id, when=now)
            return self.get_memory(memory_id)

        # source_id proximity dedup: if the same session created a very similar
        # memory within the last 60 seconds, treat it as a duplicate
        if source_session_id:
            recent_cutoff_sql = newer_than_now_expr(self.db, "created_at", "%s", "second")
            recent = self.db.fetchone(
                f"""SELECT id, content FROM memories
                   WHERE source_session_id = %s
                     AND {recent_cutoff_sql}
                   ORDER BY created_at DESC LIMIT 1""",
                (source_session_id, 60),
            )
            if recent and normalized_content[:100] == str(recent["content"]).strip()[:100]:
                return self.get_memory(recent["id"])

        tags_json = json.dumps(tags) if tags else None

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, project_id, memory_type, content, source_type,
                    source_session_id, access_count, tags,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, %s)
                """,
                (
                    memory_id,
                    project_id,
                    memory_type,
                    content,
                    source_type,
                    source_session_id,
                    tags_json,
                    now,
                    now,
                ),
            )

        self._notify_listeners()
        return self.get_memory(memory_id)

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

        Uses global deduplication - checks if any memory has the same content,
        regardless of project_id. This prevents duplicates when the same content
        is stored with different or NULL project_ids.

        Args:
            content: The content to check for
            project_id: Ignored (kept for backward compatibility)

        Returns:
            True if a memory with identical content exists
        """
        # Global deduplication: check by content directly, ignoring project_id
        # This fixes the duplicate issue where same content + different project_id
        # would create different memory IDs
        normalized_content = content.strip()
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        row = self.db.fetchone(
            f"SELECT 1 FROM memories WHERE content = %s{vis_clause} LIMIT 1",
            (normalized_content,),
        )
        return row is not None

    def get_memory_by_content(
        self, content: str, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> Memory | None:
        """Get a memory by its exact content.

        Uses global lookup - finds any memory with matching content regardless
        of project_id. This matches the behavior of content_exists().

        Args:
            content: The exact content to look up (will be normalized)
            project_id: Ignored (kept for backward compatibility)

        Returns:
            The Memory object if found, None otherwise
        """
        # Global lookup: find by content directly, ignoring project_id
        normalized_content = content.strip()
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        row = self.db.fetchone(
            f"SELECT * FROM memories WHERE content = %s{vis_clause} LIMIT 1",
            (normalized_content,),
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
            updates.append("content = %s")
            params.append(content)
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
