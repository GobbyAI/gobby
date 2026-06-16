"""Repository helpers for memory storage reads and record conversion."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

from gobby.memory.protocol import MemoryBackendProtocol, MemoryRecord
from gobby.storage.memories import LocalMemoryManager, Memory, Visibility

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
            memory_type=cast(
                Literal["fact", "preference", "pattern", "context"], record.memory_type
            ),
            content=record.content,
            created_at=record.created_at.isoformat() if record.created_at else "",
            updated_at=record.updated_at.isoformat() if record.updated_at else "",
            project_id=record.project_id,
            source_type=cast(Literal["user", "agent"], record.source_type or "agent"),
            source_session_id=record.source_session_id,
            access_count=record.access_count,
            last_accessed_at=(
                record.last_accessed_at.isoformat() if record.last_accessed_at else None
            ),
            tags=record.tags or [],
            deleted_at=record.deleted_at.isoformat() if record.deleted_at else None,
            dream_action=cast(Literal["review", "delete"] | None, record.dream_action),
            last_dreamed_at=(
                record.last_dreamed_at.isoformat() if record.last_dreamed_at else None
            ),
        )

    def count_memories(
        self, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> int:
        """Return the total number of memories using COUNT(*)."""
        return self.storage.count_memories(project_id=project_id, visibility=visibility)

    def list_memories(
        self,
        project_id: str | None = None,
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
            project_id=project_id,
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
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int | None = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        tags_all: list[str] | None = None,
        visibility: Visibility = "active",
    ) -> list[Memory]:
        """List memories via backend."""
        resolved_limit = DEFAULT_LIST_LIMIT if limit is None else limit
        records = await self.backend.list_memories(
            project_id=project_id,
            memory_type=memory_type,
            limit=resolved_limit,
            offset=offset,
            tags_all=tags_all,
            visibility=visibility,
        )
        return [self.record_to_memory(record) for record in records]

    def content_exists(
        self, content: str, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> bool:
        """Check if a memory with identical content already exists."""
        return self.storage.content_exists(content, project_id, visibility=visibility)

    async def acontent_exists(
        self, content: str, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> bool:
        """Check if a memory with identical content already exists via backend."""
        return await self.backend.content_exists(content, project_id, visibility=visibility)

    def get_memory(
        self,
        memory_id: str,
        project_id: str | None = None,
        *,
        visibility: Visibility = "active",
    ) -> Memory | None:
        """Get a specific memory by ID, optionally scoped to a project."""
        try:
            return self.storage.get_memory(memory_id, project_id=project_id, visibility=visibility)
        except ValueError:
            return None

    async def aget_memory(
        self,
        memory_id: str,
        project_id: str | None = None,
        *,
        visibility: Visibility = "active",
    ) -> Memory | None:
        """Get a specific memory by ID via backend."""
        record = await self.backend.get(memory_id, visibility=visibility)
        if record is None:
            return None
        if project_id and record.project_id and record.project_id != project_id:
            return None
        return self.record_to_memory(record)

    def find_by_prefix(
        self,
        prefix: str,
        limit: int = 5,
        project_id: str | None = None,
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
        if project_id:
            sql = (
                "SELECT * FROM memories WHERE id LIKE %s"
                + escape_clause
                + " AND (project_id = %s OR project_id IS NULL) LIMIT %s"
            )
            rows = self._db.fetchall(sql, (like_value, project_id, limit))
        else:
            sql = "SELECT * FROM memories WHERE id LIKE %s" + escape_clause + " LIMIT %s"
            rows = self._db.fetchall(sql, (like_value, limit))
        return [Memory.from_row(row) for row in rows]
