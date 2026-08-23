"""Repository helpers for memory storage reads and record conversion."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

from gobby.memory.protocol import MemoryBackendProtocol, MemoryRecord
from gobby.storage.memories import (
    ALL_MEMORIES,
    LocalMemoryManager,
    Memory,
    MemoryScope,
    Visibility,
    memory_matches_scope,
    memory_scope_predicate,
)
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


DEFAULT_LIST_LIMIT = 50


class MemoryRepository:
    """Facade over sync storage and async backend read helpers."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        storage_provider: Callable[[], LocalMemoryManager],
        backend_provider: Callable[[], MemoryBackendProtocol],
    ) -> None:
        self._db = db
        self._storage_provider = storage_provider
        self._backend_provider = backend_provider

    @property
    def storage(self) -> LocalMemoryManager:
        return self._storage_provider()

    @property
    def backend(self) -> MemoryBackendProtocol:
        return self._backend_provider()

    @staticmethod
    def record_to_memory(record: MemoryRecord) -> Memory:
        """Convert a MemoryRecord from the backend to a Memory."""
        return Memory(
            id=record.id,
            memory_type=record.memory_type,
            content=record.content,
            created_at=record.created_at or utc_now(),
            updated_at=record.updated_at or utc_now(),
            project_id=record.project_id,
            is_global=record.is_global,
            source_type=cast(Literal["user", "agent"], record.source_type or "agent"),
            source_session_id=record.source_session_id,
            rationale=record.rationale,
            source_task_id=record.source_task_id,
            created_by_agent=record.created_by_agent,
            access_count=record.access_count,
            last_accessed_at=record.last_accessed_at,
            tags=record.tags or [],
            deleted_at=record.deleted_at,
            dream_action=cast(Literal["review", "delete"] | None, record.dream_action),
            last_dreamed_at=record.last_dreamed_at,
            vector_needs_reindex=record.vector_needs_reindex,
        )

    def count_memories(
        self,
        scope: MemoryScope = ALL_MEMORIES,
        memory_type: str | None = None,
        *,
        visibility: Visibility = "active",
    ) -> int:
        """Return the total number of memories using COUNT(*)."""
        return self.storage.count_memories(
            scope=scope,
            memory_type=memory_type,
            visibility=visibility,
        )

    def list_memories(
        self,
        scope: MemoryScope = ALL_MEMORIES,
        memory_type: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        visibility: Visibility = "active",
    ) -> list[Memory]:
        """List memories with optional filtering."""
        return self.storage.list_memories(
            scope=scope,
            memory_type=memory_type,
            limit=limit,
            offset=offset,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            visibility=visibility,
        )

    async def alist_memories(
        self,
        *,
        scope: MemoryScope = ALL_MEMORIES,
        memory_type: str | None = None,
        limit: int | None = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        visibility: Visibility = "active",
    ) -> list[Memory]:
        """List memories via backend."""
        resolved_limit = DEFAULT_LIST_LIMIT if limit is None else limit
        records = await self.backend.list_memories(
            scope=scope,
            memory_type=memory_type,
            limit=resolved_limit,
            offset=offset,
            tags_all=tags_all,
            tags_any=tags_any,
            tags_none=tags_none,
            visibility=visibility,
        )
        return [self.record_to_memory(record) for record in records]

    def content_exists(
        self,
        content: str,
        scope: MemoryScope,
        *,
        visibility: Visibility = "active",
    ) -> bool:
        """Check if a memory with identical content already exists."""
        return self.storage.content_exists(content, scope, visibility=visibility)

    async def acontent_exists(
        self,
        content: str,
        scope: MemoryScope,
        *,
        visibility: Visibility = "active",
    ) -> bool:
        """Check if a memory with identical content already exists via backend."""
        return await self.backend.content_exists(content, scope, visibility=visibility)

    def get_memory(
        self,
        memory_id: str,
        scope: MemoryScope = ALL_MEMORIES,
        *,
        visibility: Visibility = "active",
    ) -> Memory | None:
        """Get a specific memory by ID in an explicit scope."""
        try:
            return self.storage.get_memory(memory_id, scope=scope, visibility=visibility)
        except ValueError:
            return None

    async def aget_memory(
        self,
        memory_id: str,
        scope: MemoryScope = ALL_MEMORIES,
        *,
        visibility: Visibility = "active",
    ) -> Memory | None:
        """Get a specific memory by ID via backend."""
        record = await self.backend.get(memory_id, visibility=visibility)
        if record is None:
            return None
        if not memory_matches_scope(record.project_id, record.is_global, scope):
            return None
        return self.record_to_memory(record)

    def find_by_prefix(
        self,
        prefix: str,
        limit: int = 5,
        scope: MemoryScope = ALL_MEMORIES,
    ) -> list[Memory]:
        """Find memories whose IDs start with the given prefix."""
        backslash = chr(92)
        percent = "%"
        underscore = "_"
        escaped = (
            prefix.replace(backslash, backslash + backslash)
            .replace(percent, backslash + percent)
            .replace(underscore, backslash + underscore)
        )
        like_value = f"{escaped}%"
        escape_clause = " ESCAPE '" + backslash + "'"
        scope_predicate, scope_params = memory_scope_predicate(scope)
        scope_clause = f" AND {scope_predicate}" if scope_predicate else ""
        sql = (
            "SELECT * FROM memories WHERE id::text LIKE %s"
            + escape_clause
            + scope_clause
            + " LIMIT %s"
        )
        rows = self._db.fetchall(sql, (like_value, *scope_params, limit))
        return [Memory.from_row(row) for row in rows]
