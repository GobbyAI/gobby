import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from gobby.memory.write_result import MemoryWriteOutcome, MemoryWriteResult
from gobby.storage.memories_base import MEMORY_PROJECTION_FENCE_LOCK_KEY, MemoryStoreBase
from gobby.storage.memories_models import (
    MEMORY_UUID_NAMESPACE,
    Memory,
    MemoryType,
    Visibility,
    validate_memory_type,
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
MAX_SUPERSEDES_IDS = 20


class DuplicateMemoryContentError(ValueError):
    """Content collides with a live memory in the same project/global scope.

    Subclasses ValueError so existing callers that treat duplicate rejection
    as a validation error keep working; the dream apply loop catches it to
    classify the collision as benign self-healing rather than a failure.
    """


def normalize_supersedes(supersedes: list[str] | None) -> list[str]:
    """Validate, canonicalize, and bound public supersession ids."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_memory_id in supersedes or []:
        try:
            memory_id = str(uuid.UUID(raw_memory_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid supersedes memory id: {raw_memory_id!r}") from exc
        if memory_id not in seen:
            normalized.append(memory_id)
            seen.add(memory_id)
    if len(normalized) > MAX_SUPERSEDES_IDS:
        raise ValueError(f"supersedes accepts at most {MAX_SUPERSEDES_IDS} unique memory ids")
    return normalized


def _memory_lock_key(memory_id: str) -> int:
    """Map a UUID to a stable signed PostgreSQL advisory-lock key."""
    raw = uuid.UUID(memory_id).int & ((1 << 64) - 1)
    return raw - (1 << 64) if raw >= (1 << 63) else raw


def _row_tags(row: Any | None) -> list[str]:
    if row is None:
        return []
    value = row["tags"]
    if isinstance(value, str):
        parsed = json.loads(value) if value else []
        return [str(tag) for tag in parsed]
    if isinstance(value, list):
        return [str(tag) for tag in value]
    return []


def _content_scope(project_id: str, is_global: bool) -> MemoryScope:
    """Return the content-dedup scope for a proposed memory."""
    if is_global:
        return MemoryScope.global_only()
    return MemoryScope.project_visible(project_id)


def render_get_memories_statement(
    memory_ids: list[str],
    scope: MemoryScope = ALL_MEMORIES,
    *,
    visibility: Visibility = "active",
) -> tuple[str, tuple[Any, ...]] | None:
    """Render ordered bulk hydration SQL for sync and async consumers."""
    if not memory_ids:
        return None

    placeholders = ", ".join("%s" for _ in memory_ids)
    vis = visibility_predicate(visibility)
    vis_clause = f" AND {vis}" if vis else ""
    scope_predicate, scope_params = memory_scope_predicate(scope)
    scope_clause = f" AND {scope_predicate}" if scope_predicate else ""
    sql = (  # Values stay parameterized; clauses come from closed internal scope enums.
        f"SELECT * FROM memories WHERE id IN ({placeholders}){scope_clause}{vis_clause}"  # nosec
    )
    return sql, (*memory_ids, *scope_params)


def map_get_memories_rows(rows: list[Any], memory_ids: list[str]) -> list[Memory]:
    """Map hydrated rows while preserving requested memory-ID order."""
    memories_by_id = {str(row["id"]): Memory.from_row(row) for row in rows}
    return [memories_by_id[memory_id] for memory_id in memory_ids if memory_id in memories_by_id]


class MemoryCrudMixin(MemoryStoreBase):
    def create_memory_with_outcome(
        self,
        content: str,
        project_id: str,
        memory_type: str | MemoryType = MemoryType.FACT,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
        memory_id: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        supersedes: list[str] | None = None,
        *,
        is_global: bool = False,
        rationale: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
    ) -> MemoryWriteResult[Memory]:
        # Validate that content is not empty
        if not content or not content.strip():
            logger.warning("Skipping memory creation: empty content provided")
            raise ValueError("Memory content cannot be empty")

        canonical_memory_type = validate_memory_type(memory_type)
        now = utc_now()
        created_at_value = to_aware_utc(created_at) if created_at is not None else now
        updated_at_value = to_aware_utc(updated_at) if updated_at is not None else now
        restore_metadata = memory_id is not None or created_at is not None or updated_at is not None
        # Normalize content for consistent ID generation (avoid duplicates from
        # whitespace differences)
        normalized_content = content.strip()
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
        final_memory_id = str(uuid.UUID(memory_id)) if memory_id else current_memory_id

        supersedes_ids = normalize_supersedes(supersedes)
        requested_tags = list(
            dict.fromkeys([*(tags or []), *(f"supersedes:{item}" for item in supersedes_ids)])
        )

        resolved_duplicate: Any | None = None
        if memory_id is None:
            scope_predicate, scope_params = memory_scope_predicate(
                _content_scope(project_id, is_global)
            )
            visible_duplicate_sql = (
                f"SELECT * FROM memories WHERE content = %s AND {scope_predicate} "  # nosec
                "AND deleted_at IS NULL "
                "ORDER BY is_global ASC, created_at ASC, id ASC LIMIT 1"
            )
            visible_duplicate = self.db.fetchone(
                visible_duplicate_sql,
                (normalized_content, *scope_params),
            )
            if visible_duplicate is not None:
                resolved_duplicate = visible_duplicate
                final_memory_id = str(visible_duplicate["id"])

        # source_id proximity dedup: if the same session created a very similar
        # memory within the last 60 seconds, treat it as a duplicate.
        if source_session_id and resolved_duplicate is None:
            recent_cutoff_sql = newer_than_now_expr(self.db, "created_at", "%s", "second")
            recent_sql = (
                f"SELECT * FROM memories WHERE source_session_id = %s "  # nosec
                "AND project_id = %s AND is_global = %s AND deleted_at IS NULL "
                f"AND {recent_cutoff_sql} ORDER BY created_at DESC, id DESC LIMIT 1"
            )
            recent = self.db.fetchone(
                recent_sql,
                (source_session_id, project_id, is_global, 60),
            )
            if recent and normalized_content == str(recent["content"]).strip():
                resolved_duplicate = recent
                final_memory_id = str(recent["id"])

        existing_row = self.db.fetchone(
            "SELECT * FROM memories WHERE id = %s",
            (final_memory_id,),
        )
        if (
            memory_id is None
            and resolved_duplicate is None
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
            existing_row = self.db.fetchone(
                "SELECT * FROM memories WHERE id = %s",
                (final_memory_id,),
            )
            if (
                existing_row is not None
                and str(existing_row["content"]).strip() != normalized_content
            ):
                raise RuntimeError(f"Memory ID collision for content: {final_memory_id}")

        changed = False
        row: Any | None = None
        with self.db.transaction() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (MEMORY_PROJECTION_FENCE_LOCK_KEY,),
            )
            advisory_ids = set(supersedes_ids)
            advisory_ids.add(current_memory_id)
            advisory_ids.add(final_memory_id)
            held_advisory_keys = {_memory_lock_key(item) for item in advisory_ids}
            for lock_key in sorted(held_advisory_keys):
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))

            locked_ids = sorted({final_memory_id, *supersedes_ids})
            placeholders = ", ".join(["%s"] * len(locked_ids))
            # SQL text interpolates generated %s placeholders only; IDs stay bound parameters.
            locked_rows = conn.execute(
                f"SELECT * FROM memories WHERE id IN ({placeholders}) ORDER BY id FOR UPDATE",  # nosec
                tuple(locked_ids),
            ).fetchall()
            rows_by_id = {str(locked_row["id"]): locked_row for locked_row in locked_rows}
            existing_row = rows_by_id.get(final_memory_id)

            existing_tags = _row_tags(existing_row)
            merged_tags = list(dict.fromkeys([*existing_tags, *requested_tags]))
            tags_json = json.dumps(merged_tags) if merged_tags else None
            pending_soft_hides: list[str] = []
            for superseded_id in supersedes_ids:
                if superseded_id == final_memory_id:
                    raise ValueError("A memory cannot supersede itself")
                superseded = rows_by_id.get(superseded_id)
                if superseded is None:
                    raise ValueError(f"Superseded memory {superseded_id} not found")
                if str(superseded["project_id"]) != project_id or bool(
                    superseded["is_global"]
                ) != bool(is_global):
                    raise ValueError(
                        f"Superseded memory {superseded_id} is outside the target scope"
                    )
                if superseded["deleted_at"] is None:
                    pending_soft_hides.append(superseded_id)
                elif f"supersedes:{superseded_id}" not in existing_tags:
                    raise ValueError(
                        f"Superseded memory {superseded_id} is hidden without matching provenance"
                    )
            if restore_metadata:
                cursor = conn.execute(
                    """
                    INSERT INTO memories (
                        id, project_id, is_global, memory_type, content, source_type,
                        source_session_id, rationale, source_task_id, created_by_agent,
                        access_count, tags, vector_needs_reindex,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, TRUE, %s, %s)
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
                        rationale = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.rationale
                            ELSE memories.rationale
                        END,
                        source_task_id = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.source_task_id
                            ELSE memories.source_task_id
                        END,
                        created_by_agent = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN excluded.created_by_agent
                            ELSE memories.created_by_agent
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
                        END,
                        vector_needs_reindex = CASE
                            WHEN excluded.updated_at > memories.updated_at
                            THEN TRUE
                            ELSE memories.vector_needs_reindex
                        END
                    RETURNING *
                    """,
                    (
                        final_memory_id,
                        project_id,
                        is_global,
                        canonical_memory_type.value,
                        normalized_content,
                        source_type,
                        source_session_id,
                        rationale,
                        source_task_id,
                        created_by_agent,
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
                        source_session_id, rationale, source_task_id, created_by_agent,
                        access_count, tags, vector_needs_reindex
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, TRUE)
                    ON CONFLICT (id) DO UPDATE SET
                        deleted_at = NULL,
                        dream_action = NULL,
                        last_dreamed_at = NULL,
                        tags = excluded.tags,
                        vector_needs_reindex = CASE
                            WHEN memories.deleted_at IS NOT NULL THEN TRUE
                            ELSE memories.vector_needs_reindex
                        END,
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
                        canonical_memory_type.value,
                        normalized_content,
                        source_type,
                        source_session_id,
                        rationale,
                        source_task_id,
                        created_by_agent,
                        tags_json,
                    ),
                )
            row = cursor.fetchone()
            if row is not None and not restore_metadata and _row_tags(row) != merged_tags:
                row = conn.execute(
                    "UPDATE memories SET tags = %s WHERE id = %s RETURNING *",
                    (tags_json, final_memory_id),
                ).fetchone()
            for superseded_id in pending_soft_hides:
                self.mark_dreamed_with_connection(
                    conn,
                    superseded_id,
                    hidden_as="delete",
                    when=now,
                )
            existing_updated_at = (
                parse_stored_datetime(existing_row["updated_at"])
                if existing_row is not None
                else None
            ) or datetime.min.replace(tzinfo=UTC)
            incoming_wins = restore_metadata and updated_at_value > existing_updated_at
            outcome: MemoryWriteOutcome
            if existing_row is None:
                outcome = "created"
            elif existing_row["deleted_at"] is not None and (not restore_metadata or incoming_wins):
                outcome = "reactivated"
            elif incoming_wins:
                outcome = "updated"
            elif memory_id is None:
                outcome = "deduped"
            else:
                outcome = "unchanged"
            changed = (
                outcome in {"created", "reactivated", "updated"}
                or existing_tags != merged_tags
                or bool(pending_soft_hides)
            )
            if changed:
                self.embedding_generation_state.append_change(
                    "memory", final_memory_id, transaction=conn
                )
                for superseded_id in pending_soft_hides:
                    self.embedding_generation_state.append_change(
                        "memory", superseded_id, is_tombstone=True, transaction=conn
                    )

        if row is None:
            raise RuntimeError(f"Memory {final_memory_id} not found after creation")

        if changed:
            self.notify_changed()
        return MemoryWriteResult(Memory.from_row(row), outcome)

    def create_memory(
        self,
        content: str,
        project_id: str,
        memory_type: str | MemoryType = MemoryType.FACT,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
        memory_id: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        supersedes: list[str] | None = None,
        *,
        is_global: bool = False,
        rationale: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
    ) -> Memory:
        """Create or deduplicate a memory while preserving the legacy payload surface."""
        return self.create_memory_with_outcome(
            content=content,
            project_id=project_id,
            memory_type=memory_type,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
            memory_id=memory_id,
            created_at=created_at,
            updated_at=updated_at,
            supersedes=supersedes,
            is_global=is_global,
            rationale=rationale,
            source_task_id=source_task_id,
            created_by_agent=created_by_agent,
        ).memory

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
            f"SELECT * FROM memories WHERE id = %s{scope_clause}{vis_clause}",  # nosec
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
        statement = render_get_memories_statement(
            memory_ids,
            scope,
            visibility=visibility,
        )
        if statement is None:
            return []
        sql, params = statement
        rows = self.db.fetchall(sql, params)
        return map_get_memories_rows(rows, memory_ids)

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
        sql = (
            f"SELECT 1 FROM memories WHERE content = %s {scope_clause}{vis_clause} "  # nosec
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        )
        row = self.db.fetchone(
            sql,
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
        sql = (
            f"SELECT * FROM memories WHERE content = %s {scope_clause}{vis_clause} "  # nosec
            "ORDER BY is_global ASC, created_at ASC, id ASC LIMIT 1"
        )
        row = self.db.fetchone(
            sql,
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
        memory_type: str | MemoryType | None = None,
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
        memory_type: str | MemoryType | None = None,
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
        memory_type: str | MemoryType | None,
        scope_clause: str,
        scope_params: tuple[str, ...],
    ) -> Memory:
        updates = []
        params: list[Any] = []
        needs_vector_reindex = False
        canonical_memory_type: MemoryType | None = None

        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("Memory content cannot be empty")
            with self.db.transaction() as conn:
                current = conn.execute(
                    f"SELECT project_id, is_global, content FROM memories WHERE id = %s{scope_clause}",  # nosec
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
                    raise DuplicateMemoryContentError(
                        "Memory content already exists in this project/global scope"
                    )
            updates.append("content = %s")
            params.append(content)
            if content != current["content"]:
                needs_vector_reindex = True
        if tags is not None:
            updates.append("tags = %s")
            params.append(json.dumps(tags))
        if memory_type is not None:
            canonical_memory_type = validate_memory_type(memory_type)
            updates.append("memory_type = %s")
            params.append(canonical_memory_type.value)
        if needs_vector_reindex:
            updates.append("vector_needs_reindex = TRUE")
        elif canonical_memory_type is not None:
            updates.append(
                "vector_needs_reindex = CASE "
                "WHEN memory_type IS DISTINCT FROM %s THEN TRUE "
                "ELSE vector_needs_reindex END"
            )
            params.append(canonical_memory_type.value)

        if not updates:
            row = self.db.fetchone(
                f"SELECT * FROM memories WHERE id = %s{scope_clause}",  # nosec
                (memory_id, *scope_params),
            )
            if not row:
                raise ValueError(f"Memory {memory_id} not found")
            return Memory.from_row(row)

        updates.append("updated_at = %s")
        params.append(utc_now())
        params.append(memory_id)
        params.extend(scope_params)

        sql = (  # nosec
            f"UPDATE memories SET {', '.join(updates)} WHERE id = %s{scope_clause} RETURNING *"
        )

        with self.db.transaction() as conn:
            cursor = conn.execute(sql, tuple(params))
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Memory {memory_id} not found")
            self.embedding_generation_state.append_change("memory", memory_id, transaction=conn)

        self.notify_changed()
        return Memory.from_row(row)

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

    def mark_vector_snapshot_reindexed(
        self,
        memory_id: str,
        content: str,
        project_id: str,
        is_global: bool,
    ) -> bool:
        """Clear repair intent only when the full scheduling identity still matches."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET vector_needs_reindex = FALSE
                WHERE id = %s
                  AND content = %s
                  AND project_id = %s
                  AND is_global = %s
                  AND deleted_at IS NULL
                """,
                (memory_id, content, project_id, is_global),
            )
            updated = cursor.rowcount
        if updated:
            self.notify_changed()
        return bool(updated)

    def reconcile_vector_snapshot_page(
        self,
        snapshots: list[tuple[str, str, str, bool]],
        reindex_ids: list[str],
    ) -> set[str]:
        """CAS-clear one rebuild page and requeue changed identities atomically."""
        cleared_ids: set[str] = set()
        changed = False
        with self.db.transaction() as conn:
            if snapshots:
                cursor = conn.execute(
                    """
                    UPDATE memories AS memory
                    SET vector_needs_reindex = FALSE
                    FROM UNNEST(
                        %s::uuid[],
                        %s::text[],
                        %s::uuid[],
                        %s::boolean[]
                    ) AS snapshot(id, content, project_id, is_global)
                    WHERE memory.id = snapshot.id
                      AND memory.content = snapshot.content
                      AND memory.project_id = snapshot.project_id
                      AND memory.is_global = snapshot.is_global
                      AND memory.deleted_at IS NULL
                    RETURNING memory.id
                    """,
                    (
                        [row[0] for row in snapshots],
                        [row[1] for row in snapshots],
                        [row[2] for row in snapshots],
                        [row[3] for row in snapshots],
                    ),
                )
                cleared_ids = {str(row["id"]) for row in cursor.fetchall()}
                changed = bool(cleared_ids)

            failed_snapshot_ids = {row[0] for row in snapshots} - cleared_ids
            ids_to_reindex = sorted({*reindex_ids, *failed_snapshot_ids})
            if ids_to_reindex:
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET vector_needs_reindex = TRUE
                    WHERE id = ANY(%s::uuid[])
                    """,
                    (ids_to_reindex,),
                )
                changed = changed or bool(cursor.rowcount)
        if changed:
            self.notify_changed()
        return cleared_ids

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
                """
                UPDATE memories
                SET project_id = %s, vector_needs_reindex = TRUE
                WHERE id = %s
                RETURNING *
                """,
                (new_project_id, memory_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Memory {memory_id} not found")
            self.embedding_generation_state.append_change("memory", memory_id, transaction=conn)

        self.notify_changed()
        return Memory.from_row(row)

    def set_memory_global(self, memory_id: str, is_global: bool) -> Memory:
        """Set cross-project visibility while preserving memory ownership."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET is_global = %s, vector_needs_reindex = TRUE
                WHERE id = %s
                RETURNING *
                """,
                (is_global, memory_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Memory {memory_id} not found")
            self.embedding_generation_state.append_change("memory", memory_id, transaction=conn)

        self.notify_changed()
        return Memory.from_row(row)

    def delete_memory(self, memory_id: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
            if cursor.rowcount == 0:
                return False
            self.embedding_generation_state.append_change(
                "memory", memory_id, is_tombstone=True, transaction=conn
            )
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
            self.embedding_generation_state.append_change(
                "memory", memory_id, is_tombstone=True, transaction=conn
            )
        self._notify_listeners()
        return True
