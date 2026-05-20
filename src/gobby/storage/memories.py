import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from gobby.memory.protocol import MediaAttachment
from gobby.storage.database import DatabaseProtocol
from gobby.storage.sql_dialect import newer_than_now_expr

# Stable namespace for deterministic memory UUIDs (uuid5)
MEMORY_UUID_NAMESPACE = uuid.UUID("a3b2c1d0-1234-5678-9abc-def012345678")

# Re-export MediaAttachment for consumers that import from this module
__all__ = ["Memory", "MemoryCrossRef", "LocalMemoryManager", "MediaAttachment"]

logger = logging.getLogger(__name__)

# Sentinel for distinguishing "not provided" from explicit None
_UNSET: Any = object()


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
    media: str | None = None  # JSON-serialized MediaAttachment data
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

        # Handle media column (may not exist in older databases)
        media = row["media"] if "media" in row.keys() else None
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
            media=media,
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
            "media": self.media,
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
    def __init__(self, db: DatabaseProtocol):
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
        media: str | None = None,
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
        existing_row = self.db.fetchone("SELECT * FROM memories WHERE id = ?", (memory_id,))
        if existing_row:
            return self.get_memory(memory_id)

        # source_id proximity dedup: if the same session created a very similar
        # memory within the last 60 seconds, treat it as a duplicate
        if source_session_id:
            recent_cutoff_sql = newer_than_now_expr(self.db, "created_at", "?", "second")
            recent = self.db.fetchone(
                f"""SELECT id, content FROM memories
                   WHERE source_session_id = ?
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
                    media, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    project_id,
                    memory_type,
                    content,
                    source_type,
                    source_session_id,
                    tags_json,
                    media,
                    now,
                    now,
                ),
            )

        self._notify_listeners()
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: str, project_id: str | None = None) -> Memory:
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
        if project_id:
            row = self.db.fetchone(
                "SELECT * FROM memories WHERE id = ? AND (project_id = ? OR project_id IS NULL)",
                (memory_id, project_id),
            )
        else:
            row = self.db.fetchone("SELECT * FROM memories WHERE id = ?", (memory_id,))
        if not row:
            raise ValueError(f"Memory {memory_id} not found")
        return Memory.from_row(row)

    def get_memories(
        self,
        memory_ids: list[str],
        project_id: str | None = None,
    ) -> list[Memory]:
        """Return multiple memories, preserving the requested order."""
        if not memory_ids:
            return []

        placeholders = ", ".join("?" for _ in memory_ids)
        if project_id:
            rows = self.db.fetchall(
                f"SELECT * FROM memories WHERE id IN ({placeholders}) "
                "AND (project_id = ? OR project_id IS NULL)",
                (*memory_ids, project_id),
            )
        else:
            rows = self.db.fetchall(
                f"SELECT * FROM memories WHERE id IN ({placeholders})",
                tuple(memory_ids),
            )

        memories_by_id = {row["id"]: Memory.from_row(row) for row in rows}
        return [
            memories_by_id[memory_id] for memory_id in memory_ids if memory_id in memories_by_id
        ]

    def memory_exists(self, memory_id: str) -> bool:
        """Check if a memory with the given ID exists."""
        row = self.db.fetchone("SELECT 1 FROM memories WHERE id = ?", (memory_id,))
        return row is not None

    def content_exists(self, content: str, project_id: str | None = None) -> bool:
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
        row = self.db.fetchone(
            "SELECT 1 FROM memories WHERE content = ? LIMIT 1",
            (normalized_content,),
        )
        return row is not None

    def get_memory_by_content(self, content: str, project_id: str | None = None) -> Memory | None:
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
        row = self.db.fetchone(
            "SELECT * FROM memories WHERE content = ? LIMIT 1",
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
        media: Any = _UNSET,  # Use sentinel to distinguish None from not-provided
    ) -> Memory:
        updates = []
        params: list[Any] = []

        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if tags is not None:
            updates.append("tags = ?")
            params.append(json.dumps(tags))
        if media is not _UNSET:  # Allow explicit None to clear media
            updates.append("media = ?")
            params.append(media)

        if not updates:
            return self.get_memory(memory_id)

        updates.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())
        params.append(memory_id)

        sql = f"UPDATE memories SET {', '.join(updates)} WHERE id = ?"  # nosec B608

        with self.db.transaction() as conn:
            cursor = conn.execute(sql, tuple(params))
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")

        self._notify_listeners()
        return self.get_memory(memory_id)

    def delete_memory(self, memory_id: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            if cursor.rowcount == 0:
                return False
        self._notify_listeners()
        return True

    def mark_pending_graph(self, memory_id: str) -> None:
        """Mark a memory as pending KG graph processing."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET graph_processed = 0 WHERE id = ?",
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
                cursor = conn.execute("UPDATE memories SET graph_processed = 0")
            else:
                cursor = conn.execute(
                    "UPDATE memories SET graph_processed = 0 WHERE project_id = ?",
                    (project_id,),
                )
            return cursor.rowcount

    def mark_graph_processed(self, memory_id: str) -> None:
        """Mark a memory as having been processed by the KG pipeline."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET graph_processed = 1 WHERE id = ?",
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
            rows = self.db.fetchall("SELECT id FROM memories LIMIT ? OFFSET ?", (limit, offset))
        else:
            rows = self.db.fetchall("SELECT id FROM memories")
        return [row["id"] for row in rows]

    def get_pending_graph_memories(self, limit: int = 20) -> list[Memory]:
        """Get memories pending KG graph processing."""
        rows = self.db.fetchall(
            "SELECT * FROM memories WHERE graph_processed = 0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        return [Memory.from_row(row) for row in rows]

    def count_memories(self, project_id: str | None = None) -> int:
        """Return the total number of memories using COUNT(*).

        When project_id is provided, includes both project-specific memories
        and global memories (project_id IS NULL) since global memories are
        accessible from any project context.
        """
        if project_id:
            row = self.db.fetchone(
                "SELECT COUNT(*) AS cnt FROM memories WHERE project_id = ? OR project_id IS NULL",
                (project_id,),
            )
        else:
            row = self.db.fetchone("SELECT COUNT(*) AS cnt FROM memories")
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
            query += " AND (project_id = ? OR project_id IS NULL)"
            params.append(project_id)

        if memory_type:
            query += " AND memory_type = ?"
            params.append(memory_type)

        # Fetch more results to allow for tag filtering
        fetch_limit = limit * 3 if (tags_all or tags_any or tags_none) else limit
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
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
                    last_accessed_at = ?
                WHERE id = ?
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
        sql = "SELECT * FROM memories WHERE content LIKE ? ESCAPE '\\'"
        params: list[Any] = [f"%{escaped_query}%"]

        if project_id:
            sql += " AND (project_id = ? OR project_id IS NULL)"
            params.append(project_id)

        # Fetch more results than needed to allow for tag filtering
        fetch_limit = limit * 3 if (tags_all or tags_any or tags_none) else limit
        sql += " ORDER BY updated_at DESC LIMIT ?"
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
                VALUES (?, ?, ?, ?)
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
            WHERE source_id = ? AND similarity >= ?
            UNION
            SELECT source_id, target_id, similarity, created_at
            FROM memory_crossrefs
            WHERE target_id = ? AND similarity >= ?
            ORDER BY similarity DESC
            LIMIT ?
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
                WHERE source_id = ? OR target_id = ?
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
                WHERE source_id IN (SELECT id FROM memories WHERE project_id = ?)
                   OR target_id IN (SELECT id FROM memories WHERE project_id = ?)
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
                WHERE (m1.project_id = ? OR m1.project_id IS NULL)
                  AND (m2.project_id = ? OR m2.project_id IS NULL)
                ORDER BY mc.similarity DESC
                LIMIT ?
                """,
                (project_id, project_id, limit),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT source_id, target_id, similarity, created_at
                FROM memory_crossrefs
                ORDER BY similarity DESC
                LIMIT ?
                """,
                (limit,),
            )

        return [MemoryCrossRef.from_row(row) for row in rows]
