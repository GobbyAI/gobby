import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import newer_than_now_expr, older_than_now_expr

# Stable namespace for deterministic memory UUIDs (uuid5)
MEMORY_UUID_NAMESPACE = uuid.UUID("a3b2c1d0-1234-5678-9abc-def012345678")

__all__ = [
    "Memory",
    "MemoryCrossRef",
    "LocalMemoryManager",
    "Visibility",
    "visibility_predicate",
]

logger = logging.getLogger(__name__)

Visibility = Literal["active", "hidden", "all"]
"""Three-state memory visibility filter: visible rows, dream-hidden rows, or both."""


def visibility_predicate(visibility: Visibility, *, column: str = "deleted_at") -> str:
    """Return a bare SQL predicate enforcing the visibility filter.

    ``"active"`` -> visible rows only, ``"hidden"`` -> dream-hidden rows only,
    ``"all"`` -> no filter (empty string). Raises ``ValueError`` on an unknown
    value so bad input fails loudly at the storage boundary rather than silently
    leaking hidden rows.
    """
    if visibility == "active":
        return f"{column} IS NULL"
    if visibility == "hidden":
        return f"{column} IS NOT NULL"
    if visibility == "all":
        return ""
    raise ValueError(f"Invalid visibility: {visibility!r}")


@dataclass
class MemoryCrossRef:
    """A link between two related memories with a similarity score."""

    source_id: str
    target_id: str
    similarity: float
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MemoryCrossRef":
        return cls(
            source_id=row["source_id"],
            target_id=row["target_id"],
            similarity=row["similarity"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "similarity": self.similarity,
            "created_at": self.created_at,
        }


@dataclass
class Memory:
    id: str
    memory_type: Literal["fact", "preference", "pattern", "context"]
    content: str
    created_at: str
    updated_at: str
    project_id: str | None = None
    source_type: Literal["user", "agent"] = "agent"
    source_session_id: str | None = None
    access_count: int = 0
    last_accessed_at: str | None = None
    tags: list[str] | None = None
    deleted_at: str | None = None  # NULL = visible; non-NULL = dream-hidden (recoverable)
    dream_action: Literal["review", "delete"] | None = None  # why dream hid the row
    last_dreamed_at: str | None = None  # cooldown cursor for the nightly active sweep
    similarity: float | None = None  # Set at search time, not persisted
    search_via: str | None = None  # Set at search time, not persisted
    ranking_score: float | None = None  # Hybrid retrieval rank, not persisted
    raw_semantic_score: float | None = None  # Raw Qdrant score, not persisted
    temporal_decay_factor: float | None = None  # Search-time decay, not persisted
    ranking_mode: str | None = None  # Search-time scoring mode, not persisted

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Memory":
        tags_json = row["tags"]
        tags = json.loads(tags_json) if tags_json else []

        raw_source_type = row["source_type"]
        source_type = cast(
            Literal["user", "agent"],
            raw_source_type if raw_source_type in ("user", "agent") else "agent",
        )

        return cls(
            id=row["id"],
            memory_type=row["memory_type"],
            content=row["content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            project_id=row["project_id"],
            source_type=source_type,
            source_session_id=row["source_session_id"],
            access_count=row["access_count"],
            last_accessed_at=row["last_accessed_at"],
            tags=tags,
            deleted_at=row.get("deleted_at"),
            dream_action=row.get("dream_action"),
            last_dreamed_at=row.get("last_dreamed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "memory_type": self.memory_type,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "project_id": self.project_id,
            "source_type": self.source_type,
            "source_session_id": self.source_session_id,
            "access_count": self.access_count,
            "last_accessed_at": self.last_accessed_at,
            "tags": self.tags,
            "deleted_at": self.deleted_at,
            "dream_action": self.dream_action,
            "last_dreamed_at": self.last_dreamed_at,
        }
        if self.similarity is not None:
            data["similarity"] = self.similarity
        if self.search_via is not None:
            data["search_via"] = self.search_via
        if self.ranking_score is not None:
            data["ranking_score"] = self.ranking_score
        if self.raw_semantic_score is not None:
            data["raw_semantic_score"] = self.raw_semantic_score
        if self.temporal_decay_factor is not None:
            data["temporal_decay_factor"] = self.temporal_decay_factor
        if self.ranking_mode is not None:
            data["ranking_mode"] = self.ranking_mode
        return data


class LocalMemoryManager:
    def __init__(self, db: HubDatabase):
        self.db = db
        self._change_listeners: list[Callable[[], Any]] = []

    def add_change_listener(self, listener: Callable[[], Any]) -> None:
        self._change_listeners.append(listener)

    def _notify_listeners(self) -> None:
        for listener in self._change_listeners:
            try:
                listener()
            except Exception as e:
                logger.error(f"Error in memory change listener: {e}")

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

    def delete_memory(self, memory_id: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
            if cursor.rowcount == 0:
                return False
        self._notify_listeners()
        return True

    def mark_dreamed(
        self,
        memory_id: str,
        *,
        hidden_as: Literal["review", "delete"] | None = None,
        when: str | None = None,
    ) -> bool:
        """Stamp ``last_dreamed_at`` and optionally soft-hide a dreamed memory.

        Direct SQL that deliberately never touches ``updated_at`` — dream review
        is GC bookkeeping, not a content edit, and bumping ``updated_at`` would
        distort recency and temporal decay. When ``hidden_as`` is ``None`` the
        row is only stamped (the ``keep`` case); when it is ``"review"`` or
        ``"delete"`` the row is soft-hidden (``deleted_at`` set, ``dream_action``
        recorded) so agent-facing reads skip it while it stays recoverable.

        Raises ``ValueError`` if the memory does not exist.
        """
        stamp = when or datetime.now(UTC).isoformat()
        if hidden_as is None:
            sql = "UPDATE memories SET last_dreamed_at = %s WHERE id = %s"
            params: tuple[Any, ...] = (stamp, memory_id)
        else:
            sql = (
                "UPDATE memories SET last_dreamed_at = %s, deleted_at = %s, "
                "dream_action = %s WHERE id = %s"
            )
            params = (stamp, stamp, hidden_as, memory_id)
        with self.db.transaction() as conn:
            cursor = conn.execute(sql, params)
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")
        self._notify_listeners()
        return True

    def restore_memory(self, memory_id: str, when: str | None = None) -> bool:
        """Reactivate a soft-hidden memory.

        Clears ``deleted_at`` and ``dream_action`` and stamps ``last_dreamed_at``
        so a freshly restored memory is not immediately re-dreamed by the next
        sweep (the cooldown applies). ``updated_at`` is left untouched.

        Raises ``ValueError`` if the memory does not exist.
        """
        stamp = when or datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET deleted_at = NULL, dream_action = NULL, "
                "last_dreamed_at = %s WHERE id = %s",
                (stamp, memory_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")
        self._notify_listeners()
        return True

    def purge_dream_hidden(self, action: str, older_than_days: int) -> list[str]:
        """Hard-delete soft-hidden rows of one ``dream_action`` past their grace.

        Returns the IDs removed so callers can reconcile secondary stores
        (Qdrant payloads, knowledge graph) after the physical rows are gone.
        """
        cutoff = older_than_now_expr(self.db, "deleted_at", "%s", "day")
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"DELETE FROM memories WHERE dream_action = %s "  # nosec B608
                f"AND deleted_at IS NOT NULL AND {cutoff} RETURNING id",
                (action, older_than_days),
            ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            self._notify_listeners()
        return ids

    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: str,
        project_id: str | None = None,
        memory_type: str | None = None,
        include_global: bool = True,
    ) -> list[Memory]:
        """Return the next page of active memories due for a dream sweep.

        Selects visible rows (``deleted_at IS NULL``) that have either never been
        dreamed or were last dreamed before ``redream_cutoff`` (the cooldown
        boundary, ``run_started_at - redream_after_hours``). Project/global and
        memory-type scoping is applied in SQL, mirroring ``_in_scope``: a
        ``project_id`` with ``include_global`` also matches global rows; without
        it only that project's rows match; a ``None`` ``project_id`` sweeps every
        row. Ordered oldest-dreamed first so the sweep drains deterministically as
        each returned page is stamped out of the next page's window.
        """
        clauses = [
            "deleted_at IS NULL",
            "(last_dreamed_at IS NULL OR last_dreamed_at < %s)",
        ]
        params: list[Any] = [redream_cutoff]
        if project_id is not None:
            if include_global:
                clauses.append("(project_id = %s OR project_id IS NULL)")
            else:
                clauses.append("project_id = %s")
            params.append(project_id)
        if memory_type is not None:
            clauses.append("memory_type = %s")
            params.append(memory_type)
        where = " AND ".join(clauses)
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT * FROM memories WHERE {where} "  # nosec B608
            "ORDER BY last_dreamed_at ASC NULLS FIRST, updated_at ASC LIMIT %s",
            tuple(params),
        )
        return [Memory.from_row(row) for row in rows]

    def mark_pending_graph(self, memory_id: str) -> None:
        """Mark a memory as pending KG graph processing."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET graph_processed = FALSE WHERE id = %s",
                (memory_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory not found: {memory_id}")

    def mark_pending_graphs(self, project_id: str | None = None) -> int:
        """Mark multiple memories as pending KG graph processing.

        When ``project_id`` is provided, only memories in that project are reset.
        When omitted, all memories are reset.
        """
        with self.db.transaction() as conn:
            if project_id is None:
                cursor = conn.execute("UPDATE memories SET graph_processed = FALSE")
            else:
                cursor = conn.execute(
                    "UPDATE memories SET graph_processed = FALSE WHERE project_id = %s",
                    (project_id,),
                )
            return cursor.rowcount

    def mark_graph_processed(self, memory_id: str) -> None:
        """Mark a memory as having been processed by the KG pipeline."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET graph_processed = TRUE WHERE id = %s",
                (memory_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory not found: {memory_id}")

    def list_all_ids(self, *, limit: int | None = None, offset: int = 0) -> list[str]:
        """Return memory IDs from the database.

        Args:
            limit: Max number of IDs to return. None returns all.
            offset: Number of rows to skip before returning results.
        """
        if limit is not None:
            rows = self.db.fetchall("SELECT id FROM memories LIMIT %s OFFSET %s", (limit, offset))
        else:
            rows = self.db.fetchall("SELECT id FROM memories")
        return [row["id"] for row in rows]

    def get_pending_graph_memories(self, limit: int = 20) -> list[Memory]:
        """Get memories pending KG graph processing."""
        rows = self.db.fetchall(
            "SELECT * FROM memories WHERE graph_processed IS FALSE ORDER BY created_at ASC LIMIT %s",
            (limit,),
        )
        return [Memory.from_row(row) for row in rows]

    def count_memories(
        self, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> int:
        """Return the total number of memories using COUNT(*).

        When project_id is provided, includes both project-specific memories
        and global memories (project_id IS NULL) since global memories are
        accessible from any project context.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("(project_id = %s OR project_id IS NULL)")
            params.append(project_id)
        vis = visibility_predicate(visibility)
        if vis:
            clauses.append(vis)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.db.fetchone(f"SELECT COUNT(*) AS cnt FROM memories{where}", tuple(params))
        return row["cnt"] if row else 0

    def list_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        *,
        visibility: Visibility = "active",
    ) -> list[Memory]:
        """
        List memories with optional filtering.

        Args:
            project_id: Filter by project ID (or None for global)
            memory_type: Filter by memory type
            limit: Maximum number of results
            offset: Number of results to skip
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of these tags

        Returns:
            List of matching memories
        """
        query = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []

        if project_id:
            query += " AND (project_id = %s OR project_id IS NULL)"
            params.append(project_id)

        if memory_type:
            query += " AND memory_type = %s"
            params.append(memory_type)

        vis = visibility_predicate(visibility)
        if vis:
            query += f" AND {vis}"

        # Fetch more results to allow for tag filtering
        fetch_limit = limit * 3 if (tags_all or tags_any or tags_none) else limit
        query += " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
        params.extend([fetch_limit, offset])

        rows = self.db.fetchall(query, tuple(params))
        memories = [Memory.from_row(row) for row in rows]

        # Apply tag filters
        if tags_all or tags_any or tags_none:
            memories = self._filter_by_tags(memories, tags_all, tags_any, tags_none)

        return memories[:limit]

    def update_access_stats(self, memory_id: str, accessed_at: str) -> None:
        """
        Update access count and last accessed timestamp for a memory.

        Args:
            memory_id: Memory ID to update
            accessed_at: ISO format timestamp of access
        """
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed_at = %s
                WHERE id = %s
                """,
                (accessed_at, memory_id),
            )

    def search_memories(
        self,
        query_text: str,
        project_id: str | None = None,
        limit: int = 20,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        *,
        visibility: Visibility = "active",
    ) -> list[Memory]:
        """
        Search memories by content with optional tag filtering.

        Args:
            query_text: Text to search for in memory content
            project_id: Optional project ID to filter by
            limit: Maximum number of results
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of these tags

        Returns:
            List of matching memories
        """
        # Escape LIKE wildcards in query_text
        escaped_query = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = "SELECT * FROM memories WHERE content LIKE %s ESCAPE '\\'"
        params: list[Any] = [f"%{escaped_query}%"]

        if project_id:
            sql += " AND (project_id = %s OR project_id IS NULL)"
            params.append(project_id)

        vis = visibility_predicate(visibility)
        if vis:
            sql += f" AND {vis}"

        # Fetch more results than needed to allow for tag filtering
        fetch_limit = limit * 3 if (tags_all or tags_any or tags_none) else limit
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(fetch_limit)

        rows = self.db.fetchall(sql, tuple(params))
        memories = [Memory.from_row(row) for row in rows]

        # Apply tag filters in Python
        if tags_all or tags_any or tags_none:
            memories = self._filter_by_tags(memories, tags_all, tags_any, tags_none)

        return memories[:limit]

    def _filter_by_tags(
        self,
        memories: list[Memory],
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> list[Memory]:
        """
        Filter memories by tag criteria.

        Args:
            memories: List of memories to filter
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of these tags

        Returns:
            Filtered list of memories
        """
        result = []
        for memory in memories:
            memory_tags = set(memory.tags) if memory.tags else set()

            # Check tags_all: memory must have ALL specified tags
            if tags_all:
                if not set(tags_all).issubset(memory_tags):
                    continue

            # Check tags_any: memory must have at least ONE specified tag
            if tags_any:
                if not memory_tags.intersection(tags_any):
                    continue

            # Check tags_none: memory must have NONE of the specified tags
            if tags_none:
                if memory_tags.intersection(tags_none):
                    continue

            result.append(memory)

        return result

    # --- Cross-reference methods ---

    def create_crossref(
        self,
        source_id: str,
        target_id: str,
        similarity: float,
    ) -> MemoryCrossRef:
        """
        Create a cross-reference link between two memories.

        Args:
            source_id: The source memory ID
            target_id: The target memory ID
            similarity: Similarity score (0.0 to 1.0)

        Returns:
            The created MemoryCrossRef

        Note:
            If the crossref already exists, it will be updated with
            the new similarity score.
        """
        now = datetime.now(UTC).isoformat()

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO memory_crossrefs (source_id, target_id, similarity, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(source_id, target_id) DO UPDATE SET
                    similarity = excluded.similarity
                """,
                (source_id, target_id, similarity, now),
            )

        return MemoryCrossRef(
            source_id=source_id,
            target_id=target_id,
            similarity=similarity,
            created_at=now,
        )

    def get_crossrefs(
        self,
        memory_id: str,
        limit: int = 10,
        min_similarity: float = 0.0,
    ) -> list[MemoryCrossRef]:
        """
        Get cross-references for a memory (both as source and target).

        Args:
            memory_id: The memory ID to find links for
            limit: Maximum number of results
            min_similarity: Minimum similarity threshold

        Returns:
            List of MemoryCrossRef objects, sorted by similarity descending
        """
        # Get crossrefs where this memory is the source
        rows = self.db.fetchall(
            """
            SELECT source_id, target_id, similarity, created_at
            FROM memory_crossrefs
            WHERE source_id = %s AND similarity >= %s
            UNION
            SELECT source_id, target_id, similarity, created_at
            FROM memory_crossrefs
            WHERE target_id = %s AND similarity >= %s
            ORDER BY similarity DESC
            LIMIT %s
            """,
            (memory_id, min_similarity, memory_id, min_similarity, limit),
        )

        return [MemoryCrossRef.from_row(row) for row in rows]

    def delete_crossrefs(self, memory_id: str) -> int:
        """
        Delete all cross-references involving a memory.

        Called automatically when a memory is deleted due to CASCADE,
        but can be called manually for cleanup.

        Args:
            memory_id: The memory ID to delete crossrefs for

        Returns:
            Number of crossrefs deleted
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM memory_crossrefs
                WHERE source_id = %s OR target_id = %s
                """,
                (memory_id, memory_id),
            )
            return cursor.rowcount

    def delete_project_crossrefs(self, project_id: str) -> int:
        """Delete all cross-references for memories belonging to a project.

        Args:
            project_id: The project ID whose crossrefs should be deleted.

        Returns:
            Number of crossrefs deleted.
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM memory_crossrefs
                WHERE source_id IN (SELECT id FROM memories WHERE project_id = %s)
                   OR target_id IN (SELECT id FROM memories WHERE project_id = %s)
                """,
                (project_id, project_id),
            )
            return cursor.rowcount

    def get_all_crossrefs(
        self,
        project_id: str | None = None,
        limit: int = 1000,
    ) -> list[MemoryCrossRef]:
        """
        Get all cross-references, optionally filtered by project.

        Useful for building memory graphs.

        Args:
            project_id: Filter to memories in this project
            limit: Maximum number of results

        Returns:
            List of MemoryCrossRef objects
        """
        if project_id:
            # Join with memories to filter by project
            rows = self.db.fetchall(
                """
                SELECT DISTINCT mc.source_id, mc.target_id, mc.similarity, mc.created_at
                FROM memory_crossrefs mc
                JOIN memories m1 ON mc.source_id = m1.id
                JOIN memories m2 ON mc.target_id = m2.id
                WHERE (m1.project_id = %s OR m1.project_id IS NULL)
                  AND (m2.project_id = %s OR m2.project_id IS NULL)
                ORDER BY mc.similarity DESC
                LIMIT %s
                """,
                (project_id, project_id, limit),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT source_id, target_id, similarity, created_at
                FROM memory_crossrefs
                ORDER BY similarity DESC
                LIMIT %s
                """,
                (limit,),
            )

        return [MemoryCrossRef.from_row(row) for row in rows]
