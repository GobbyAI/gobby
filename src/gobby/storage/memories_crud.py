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
from gobby.storage.memories_scope import (
    ALL_MEMORIES,
    MemoryScope,
    memory_scope_predicate,
)
from gobby.storage.sql_dialect import newer_than_now_expr
from gobby.utils.datetime import parse_stored_datetime, to_aware_utc, utc_now

logger = logging.getLogger(__name__)


def _content_scope(project_id: str, is_global: bool) -> MemoryScope:
    """Return the content-dedup scope for a proposed memory."""
    if is_global:
        return MemoryScope.global_only()
    return MemoryScope.project_visible(project_id)


class MemoryCrudMixin(MemoryStoreBase):
    def create_memory(
        self,
        content: str,
        project_id: str,
        memory_type: str = "fact",
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
        memory_id: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        *,
        is_global: bool = False,
    ) -> Memory:
        # Validate that content is not empty
        if not content or not content.strip():
            logger.warning("Skipping memory creation: empty content provided")
            raise ValueError("Memory content cannot be empty")

        now = utc_now()
        created_at_value = to_aware_utc(created_at) if created_at is not None else now
        updated_at_value = to_aware_utc(updated_at) if updated_at is not None else now
        restore_metadata = memory_id is not None or created_at is not None or updated_at is not None
        # Normalize content for consistent ID generation (avoid duplicates from
        # whitespace differences)
        normalized_content = content.strip()
        if memory_id:
            final_memory_id = memory_id
        else:
            current_memory_id_seed = json.dumps(
                {
                    "content": normalized_content,
                    "project_id": project_id,
                    "is_global": is_global,
                },
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
                scope_predicate, scope_params = memory_scope_predicate(
                    _content_scope(project_id, is_global)
                )
                visible_duplicate = conn.execute(
                    f"""
                    SELECT * FROM memories
                     WHERE content = %s
                       AND {scope_predicate}
                       AND deleted_at IS NULL
                     ORDER BY is_global ASC,
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
                         AND project_id = %s
                         AND is_global = %s
                         AND deleted_at IS NULL
                         AND {recent_cutoff_sql}
                       ORDER BY created_at DESC, id DESC LIMIT 1""",
                    (source_session_id, project_id, is_global, 60),
                ).fetchone()
                if recent and normalized_content == str(recent["content"]).strip():
                    return Memory.from_row(recent)

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
                        "is_global": is_global,
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
            if restore_metadata:
                cursor = conn.execute(
                    """
                    INSERT INTO memories (
                        id, project_id, is_global, memory_type, content, source_type,
                        source_session_id, access_count, tags,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        project_id = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.project_id
                            ELSE memories.project_id
                        END,
                        is_global = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.is_global
                            ELSE memories.is_global
                        END,
                        memory_type = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.memory_type
                            ELSE memories.memory_type
                        END,
                        content = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.content
                            ELSE memories.content
                        END,
                        source_type = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.source_type
                            ELSE memories.source_type
                        END,
                        source_session_id = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.source_session_id
                            ELSE memories.source_session_id
                        END,
                        tags = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.tags
                            ELSE memories.tags
                        END,
                        updated_at = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.updated_at
                            ELSE memories.updated_at
                        END,
                        deleted_at = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              AND excluded.updated_at > memories.updated_at
                            THEN NULL
                            ELSE memories.deleted_at
                        END,
                        dream_action = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              AND excluded.updated_at > memories.updated_at
                            THEN NULL
                            ELSE memories.dream_action
                        END,
                        last_dreamed_at = CASE
                            WHEN memories.deleted_at IS NOT NULL
                              AND excluded.updated_at > memories.updated_at
                            THEN NULL
                            ELSE memories.last_dreamed_at
                        END
                    RETURNING *
                    """,
                    (
                        final_memory_id,
                        project_id,
                        is_global,
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
                        id, project_id, is_global, memory_type, content, source_type,
                        source_session_id, access_count, tags,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)
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
                        is_global,
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
            changed = existing_row is None
            if restore_metadata and existing_row is not None:
                existing_updated_at = parse_stored_datetime(
                    existing_row["updated_at"]
                ) or datetime.min.replace(tzinfo=UTC)
                changed = updated_at_value > existing_updated_at
            elif existing_row is not None:
                changed = existing_row["deleted_at"] is not None

        if row is None:
            raise RuntimeError(f"Memory {final_memory_id} not found after creation")

        if changed:
            self._notify_listeners()
        return Memory.from_row(row)

    def get_memory(
        self,
        memory_id: str,
        scope: MemoryScope = ALL_MEMORIES,
        *,
        visibility: Visibility = "active",
    ) -> Memory:
        """Get a memory by ID in an explicit ownership/visibility scope."""
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        scope_predicate, scope_params = memory_scope_predicate(scope)
        scope_clause = f" AND {scope_predicate}" if scope_predicate else ""
        row = self.db.fetchone(
            f"SELECT * FROM memories WHERE id = %s{scope_clause}{vis_clause}",
            (memory_id, *scope_params),
        )
        if not row:
            raise ValueError(f"Memory {memory_id} not found")
        return Memory.from_row(row)

    def get_memories(
        self,
        memory_ids: list[str],
        scope: MemoryScope = ALL_MEMORIES,
        *,
        visibility: Visibility = "active",
    ) -> list[Memory]:
        """Return multiple memories, preserving the requested order."""
        if not memory_ids:
            return []

        placeholders = ", ".join("%s" for _ in memory_ids)
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        scope_predicate, scope_params = memory_scope_predicate(scope)
        scope_clause = f" AND {scope_predicate}" if scope_predicate else ""
        rows = self.db.fetchall(
            f"SELECT * FROM memories WHERE id IN ({placeholders}){scope_clause}{vis_clause}",
            (*memory_ids, *scope_params),
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
        self,
        content: str,
        scope: MemoryScope,
        *,
        visibility: Visibility = "active",
    ) -> bool:
        """Check if a memory with identical content already exists.

        The caller selects project-visible, project-only, global-only, or all scope.
        """
        normalized_content = content.strip()
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        scope_predicate, scope_params = memory_scope_predicate(scope)
        scope_clause = f" AND {scope_predicate}" if scope_predicate else ""
        row = self.db.fetchone(
            f"""
            SELECT 1 FROM memories
             WHERE content = %s
               {scope_clause}{vis_clause}
             ORDER BY created_at ASC, id ASC
             LIMIT 1
            """,  # nosec B608
            (normalized_content, *scope_params),
        )
        return row is not None

    def get_memory_by_content(
        self,
        content: str,
        scope: MemoryScope,
        *,
        visibility: Visibility = "active",
    ) -> Memory | None:
        """Get a memory by its exact content.

        Uses the same explicit scope as :meth:`content_exists`.
        """
        normalized_content = content.strip()
        vis = visibility_predicate(visibility)
        vis_clause = f" AND {vis}" if vis else ""
        scope_predicate, scope_params = memory_scope_predicate(scope)
        scope_clause = f" AND {scope_predicate}" if scope_predicate else ""
        row = self.db.fetchone(
            f"""
            SELECT * FROM memories
             WHERE content = %s
               {scope_clause}{vis_clause}
             ORDER BY is_global ASC,
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
        project_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        memory_type: str | None = None,
    ) -> Memory:
        """Update a memory only when it is visible in the requested project scope."""
        scope_predicate, scope_params = memory_scope_predicate(
            MemoryScope.project_visible(project_id)
        )
        scope_clause = f" AND {scope_predicate}"
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
                    f"SELECT project_id, is_global, content FROM memories WHERE id = %s{scope_clause}",
                    (memory_id, *scope_params),
                ).fetchone()
                if current is None:
                    raise ValueError(f"Memory {memory_id} not found")
                duplicate = conn.execute(
                    """
                    SELECT id FROM memories
                     WHERE content = %s
                       AND project_id = %s
                       AND is_global = %s
                       AND id != %s
                     LIMIT 1
                    """,
                    (content, current["project_id"], current["is_global"], memory_id),
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

    def mark_vector_reindex_needed(self, memory_id: str) -> None:
        """Mark one memory for a retryable vector projection repair."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET vector_needs_reindex = TRUE WHERE id = %s",
                (memory_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")

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

    def move_memory(self, memory_id: str, new_project_id: str) -> Memory:
        """Move memory ownership while preserving its visibility."""
        with self.db.transaction() as conn:
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
        return self.get_memory(memory_id)

    def set_memory_global(self, memory_id: str, is_global: bool) -> Memory:
        """Set cross-project visibility while preserving memory ownership."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET is_global = %s WHERE id = %s",
                (is_global, memory_id),
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

    def delete_memory_scoped(self, memory_id: str, project_id: str) -> bool:
        """Delete a memory only when it is visible in the requested project scope."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE id = %s AND (project_id = %s OR is_global IS TRUE)",
                (memory_id, project_id),
            )
            if cursor.rowcount == 0:
                return False
        self._notify_listeners()
        return True
