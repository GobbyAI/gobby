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
from gobby.utils.datetime import parse_stored_datetime, to_aware_utc, utc_now

logger = logging.getLogger(__name__)


def _content_scope_predicate(project_id: str | None) -> tuple[str, tuple[str, ...]]:
    """Return the content-dedup scope visible from a target project."""
    if project_id is None:
        return "project_id IS NULL", ()
    return "(project_id = %s OR project_id IS NULL)", (project_id,)


class MemoryCrudMixin(MemoryStoreBase):
    def create_memory(
        self,
        content: str,
        memory_type: str = "fact",
        project_id: str | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
        memory_id: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> Memory:
        # Validate that content is not empty
        if not content or not content.strip():
            logger.warning("Skipping memory creation: empty content provided")
            raise ValueError("Memory content cannot be empty")

        now = utc_now()
        created_at_value = to_aware_utc(created_at) if created_at is not None else now
        updated_at_value = to_aware_utc(updated_at) if updated_at is not None else now
        sync_metadata = memory_id is not None or created_at is not None or updated_at is not None
        # Normalize content for consistent ID generation (avoid duplicates from
        # whitespace differences)
        normalized_content = content.strip()
        if memory_id:
            final_memory_id = memory_id
        else:
            legacy_memory_id = str(uuid.uuid5(MEMORY_UUID_NAMESPACE, normalized_content))
            current_memory_id_seed = json.dumps(
                {"content": normalized_content, "project_id": project_id},
                sort_keys=True,
                separators=(",", ":"),
            )
            current_memory_id = str(uuid.uuid5(MEMORY_UUID_NAMESPACE, current_memory_id_seed))
            final_memory_id = current_memory_id

        tags_json = json.dumps(tags) if tags else None

        changed = False
        row: Any | None = None
        with self.db.transaction() as conn:
            if memory_id is None:
                scope_predicate, scope_params = _content_scope_predicate(project_id)
                visible_duplicate = conn.execute(
                    f"""
                    SELECT * FROM memories
                     WHERE content = %s
                       AND {scope_predicate}
                       AND deleted_at IS NULL
                     ORDER BY CASE WHEN project_id IS NULL THEN 1 ELSE 0 END,
                              created_at ASC,
                              id ASC
                     LIMIT 1
                    """,  # nosec B608
                    (normalized_content, *scope_params),
                ).fetchone()
                if visible_duplicate is not None:
                    return Memory.from_row(visible_duplicate)

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

            if memory_id is None and project_id is None:
                final_memory_id = legacy_memory_id
            elif memory_id is None:
                legacy_row = conn.execute(
                    """
                    SELECT 1 FROM memories
                     WHERE id = %s
                       AND project_id IS NOT DISTINCT FROM %s
                    """,
                    (legacy_memory_id, project_id),
                ).fetchone()
                if legacy_row is not None:
                    final_memory_id = legacy_memory_id

            existing_row = conn.execute(
                "SELECT content, deleted_at, updated_at FROM memories WHERE id = %s",
                (final_memory_id,),
            ).fetchone()
            if (
                memory_id is None
                and existing_row is not None
                and str(existing_row["content"]).strip() != normalized_content
            ):
                collision_seed = json.dumps(
                    {
                        "content": normalized_content,
                        "project_id": project_id,
                        "id_collision": final_memory_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                final_memory_id = str(uuid.uuid5(MEMORY_UUID_NAMESPACE, collision_seed))
                existing_row = conn.execute(
                    "SELECT content, deleted_at, updated_at FROM memories WHERE id = %s",
                    (final_memory_id,),
                ).fetchone()
                if (
                    existing_row is not None
                    and str(existing_row["content"]).strip() != normalized_content
                ):
                    raise RuntimeError(f"Memory ID collision for content: {final_memory_id}")
            if sync_metadata:
                cursor = conn.execute(
                    """
                    INSERT INTO memories (
                        id, project_id, memory_type, content, source_type,
                        source_session_id, access_count, tags,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        project_id = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              OR excluded.updated_at > memories.updated_at
                            THEN excluded.project_id
                            ELSE memories.project_id
                        END,
                        memory_type = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              OR excluded.updated_at > memories.updated_at
                            THEN excluded.memory_type
                            ELSE memories.memory_type
                        END,
                        content = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              OR excluded.updated_at > memories.updated_at
                            THEN excluded.content
                            ELSE memories.content
                        END,
                        source_type = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              OR excluded.updated_at > memories.updated_at
                            THEN excluded.source_type
                            ELSE memories.source_type
                        END,
                        source_session_id = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              OR excluded.updated_at > memories.updated_at
                            THEN excluded.source_session_id
                            ELSE memories.source_session_id
                        END,
                        tags = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              OR excluded.updated_at > memories.updated_at
                            THEN excluded.tags
                            ELSE memories.tags
                        END,
                        created_at = CASE
                            WHEN memories.created_at <= excluded.created_at
                            THEN memories.created_at
                            ELSE excluded.created_at
                        END,
                        updated_at = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              OR excluded.updated_at > memories.updated_at
                            THEN excluded.updated_at
                            ELSE memories.updated_at
                        END,
                        deleted_at = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              AND excluded.updated_at >= memories.updated_at
                            THEN NULL
                            ELSE memories.deleted_at
                        END,
                        dream_action = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              AND excluded.updated_at >= memories.updated_at
                            THEN NULL
                            ELSE memories.dream_action
                        END,
                        last_dreamed_at = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              AND excluded.updated_at >= memories.updated_at
                            THEN NULL
                            ELSE memories.last_dreamed_at
                        END
                    RETURNING *
                    """,
                    (
                        final_memory_id,
                        project_id,
                        memory_type,
                        normalized_content,
                        source_type,
                        source_session_id,
                        tags_json,
                        created_at_value,
                        updated_at_value,
                    ),
                )
            else:
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
                        final_memory_id,
                        project_id,
                        memory_type,
                        normalized_content,
                        source_type,
                        source_session_id,
                        tags_json,
                        created_at_value,
                        updated_at_value,
                    ),
                )
            row = cursor.fetchone()
            changed = existing_row is None or existing_row["deleted_at"] is not None
            if sync_metadata and existing_row is not None:
                existing_updated_at = parse_stored_datetime(
                    existing_row["updated_at"]
                ) or datetime.min.replace(tzinfo=UTC)
                changed = changed or updated_at_value > existing_updated_at

        if row is None:
            raise RuntimeError(f"Memory {final_memory_id} not found after creation")

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

        Project-scoped lookups include visible global memories. Global lookups
        remain limited to the global scope.

        Args:
            content: The content to check for
            project_id: Project scope to check plus globals. ``None`` checks globals only.

        Returns:
            True if a memory with identical content exists
        """
        normalized_content = content.strip()
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        scope_predicate, scope_params = _content_scope_predicate(project_id)
        row = self.db.fetchone(
            f"""
            SELECT 1 FROM memories
             WHERE content = %s
               AND {scope_predicate}{vis_clause}
             ORDER BY created_at ASC, id ASC
             LIMIT 1
            """,  # nosec B608
            (normalized_content, *scope_params),
        )
        return row is not None

    def get_memory_by_content(
        self, content: str, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> Memory | None:
        """Get a memory by its exact content.

        Uses project-plus-global lookup, matching the behavior of content_exists().

        Args:
            content: The exact content to look up (will be normalized)
            project_id: Project scope to check plus globals. ``None`` checks globals only.

        Returns:
            The Memory object if found, None otherwise
        """
        normalized_content = content.strip()
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        scope_predicate, scope_params = _content_scope_predicate(project_id)
        row = self.db.fetchone(
            f"""
            SELECT * FROM memories
             WHERE content = %s
               AND {scope_predicate}{vis_clause}
             ORDER BY CASE WHEN project_id IS NULL THEN 1 ELSE 0 END,
                      created_at ASC,
                      id ASC
             LIMIT 1
            """,  # nosec B608
            (normalized_content, *scope_params),
        )
        if row:
            return Memory.from_row(row)
        return None

    def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> Memory:
        return self._update_memory_in_scope(
            memory_id=memory_id,
            content=content,
            tags=tags,
            memory_type=memory_type,
            scope_clause="",
            scope_params=(),
        )

    def update_memory_scoped(
        self,
        memory_id: str,
        project_id: str | None,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> Memory:
        """Update a memory only when it is visible in the requested project scope."""
        scope_clause = (
            " AND project_id IS NULL"
            if project_id is None
            else " AND (project_id = %s OR project_id IS NULL)"
        )
        scope_params = () if project_id is None else (project_id,)
        return self._update_memory_in_scope(
            memory_id=memory_id,
            content=content,
            tags=tags,
            memory_type=memory_type,
            scope_clause=scope_clause,
            scope_params=scope_params,
        )

    def _update_memory_in_scope(
        self,
        memory_id: str,
        content: str | None,
        tags: list[str] | None,
        memory_type: str | None,
        scope_clause: str,
        scope_params: tuple[str, ...],
    ) -> Memory:
        updates = []
        params: list[Any] = []

        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("Memory content cannot be empty")
            with self.db.transaction() as conn:
                current = conn.execute(
                    f"SELECT project_id, content FROM memories WHERE id = %s{scope_clause}",
                    (memory_id, *scope_params),
                ).fetchone()
                if current is None:
                    raise ValueError(f"Memory {memory_id} not found")
                duplicate = conn.execute(
                    """
                    SELECT id FROM memories
                     WHERE content = %s
                       AND project_id IS NOT DISTINCT FROM %s
                       AND id != %s
                     LIMIT 1
                    """,
                    (content, current["project_id"], memory_id),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError("Memory content already exists in this project/global scope")
            updates.append("content = %s")
            params.append(content)
            if content != current["content"]:
                updates.append("vector_needs_reindex = TRUE")
        if tags is not None:
            updates.append("tags = %s")
            params.append(json.dumps(tags))
        if memory_type is not None:
            updates.append("memory_type = %s")
            params.append(memory_type)

        if not updates:
            row = self.db.fetchone(
                f"SELECT * FROM memories WHERE id = %s{scope_clause}",
                (memory_id, *scope_params),
            )
            if not row:
                raise ValueError(f"Memory {memory_id} not found")
            return Memory.from_row(row)

        updates.append("updated_at = %s")
        params.append(utc_now())
        params.append(memory_id)
        params.extend(scope_params)

        sql = (  # nosec B608
            f"UPDATE memories SET {', '.join(updates)} WHERE id = %s{scope_clause}"
        )

        with self.db.transaction() as conn:
            cursor = conn.execute(sql, tuple(params))
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")

        self._notify_listeners()
        return self.get_memory(memory_id)

    def list_vector_reindex_ids(self) -> list[str]:
        """Return memories whose stored content is newer than their vector."""
        rows = self.db.fetchall(
            "SELECT id FROM memories WHERE vector_needs_reindex IS TRUE ORDER BY id"
        )
        return [str(row["id"]) for row in rows]

    def mark_vectors_reindexed(self, indexed_content: dict[str, str]) -> int:
        """Clear stale state only when the indexed content is still current."""
        if not indexed_content:
            return 0
        cleared = 0
        with self.db.transaction() as conn:
            for memory_id, content in indexed_content.items():
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET vector_needs_reindex = (content IS DISTINCT FROM %s)
                    WHERE id = %s
                    RETURNING content IS NOT DISTINCT FROM %s AS content_matched
                    """,
                    (content, memory_id, content),
                )
                row = cursor.fetchone()
                if row is not None and row["content_matched"]:
                    cleared += 1
        return cleared

    def update_memory_project(self, memory_id: str, project_id: str) -> Memory:
        """Update a memory's project assignment and notify storage listeners."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET project_id = %s, updated_at = %s WHERE id = %s",
                (project_id, utc_now(), memory_id),
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

    def delete_memory_scoped(self, memory_id: str, project_id: str | None) -> bool:
        """Delete a memory only when it is visible in the requested project scope."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE id = %s AND (project_id = %s OR project_id IS NULL)",
                (memory_id, project_id),
            )
            if cursor.rowcount == 0:
                return False
        self._notify_listeners()
        return True
