"""Storage adapter for MemoryBackendProtocol.

Wraps an existing LocalMemoryManager instance to provide the async
MemoryBackendProtocol interface. Used by MemoryManager to expose PostgreSQL
hub-backed memory storage through the backend protocol.

Unlike the removed standalone backend, this adapter does NOT create its own
LocalMemoryManager — it reuses the one owned by MemoryManager, eliminating the
duplicate-instance problem.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from gobby.memory.protocol import (
    MemoryCapability,
    MemoryQuery,
    MemoryRecord,
)
from gobby.memory.write_result import MemoryWriteResult
from gobby.storage.memories import ALL_MEMORIES, LocalMemoryManager, MemoryScope, Visibility
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.datetime import parse_stored_datetime, utc_now


class StorageAdapter:
    """Adapts LocalMemoryManager to the async MemoryBackendProtocol interface."""

    def __init__(
        self,
        storage: LocalMemoryManager,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ):
        self._storage = storage
        self._run_db = run_db

    async def _run_storage(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    def capabilities(self) -> set[MemoryCapability]:
        return {
            MemoryCapability.CREATE,
            MemoryCapability.READ,
            MemoryCapability.UPDATE,
            MemoryCapability.DELETE,
            MemoryCapability.SEARCH_TEXT,
            MemoryCapability.SEARCH,
            MemoryCapability.TAGS,
            MemoryCapability.LIST,
            MemoryCapability.REMEMBER,
            MemoryCapability.RECALL,
            MemoryCapability.FORGET,
        }

    async def create(
        self,
        content: str,
        project_id: str = PERSONAL_PROJECT_ID,
        memory_type: str = "fact",
        is_global: bool = False,
        user_id: str | None = None,
        tags: list[str] | None = None,
        supersedes: list[str] | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        rationale: str | None = None,
        source_task_id: str | None = None,
        created_by_agent: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryWriteResult[MemoryRecord]:
        result = await self._run_storage(
            self._storage.create_memory_with_outcome,
            content=content,
            memory_type=memory_type,
            project_id=project_id,
            is_global=is_global,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
            supersedes=supersedes,
            rationale=rationale,
            source_task_id=source_task_id,
            created_by_agent=created_by_agent,
        )
        return MemoryWriteResult(
            self._to_record(result.memory, user_id=user_id, metadata=metadata),
            result.outcome,
        )

    async def get(
        self, memory_id: str, *, visibility: Visibility = "active"
    ) -> MemoryRecord | None:
        try:
            memory = await self._run_storage(
                self._storage.get_memory, memory_id, visibility=visibility
            )
            return self._to_record(memory)
        except ValueError:
            return None

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        memory = await self._run_storage(
            self._storage.update_memory,
            memory_id=memory_id,
            content=content,
            tags=tags,
        )
        if memory is None:
            raise ValueError(f"Memory not found: {memory_id}")
        return self._to_record(memory)

    async def delete(self, memory_id: str) -> bool:
        return cast(bool, await self._run_storage(self._storage.delete_memory, memory_id))

    async def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        memories = await self._run_storage(
            self._storage.search_memories,
            query_text=query.text,
            scope=query.scope,
            limit=query.limit,
            tags_all=query.tags_all,
            tags_any=query.tags_any,
            tags_none=query.tags_none,
            visibility=query.visibility,
        )
        if query.memory_type is not None:
            memories = [m for m in memories if m.memory_type == query.memory_type]
        return [self._to_record(m) for m in memories]

    async def list_memories(
        self,
        scope: MemoryScope = ALL_MEMORIES,
        user_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_none: list[str] | None = None,
        *,
        visibility: Visibility = "active",
    ) -> list[MemoryRecord]:
        memories = await self._run_storage(
            self._storage.list_memories,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
            offset=offset,
            tags_all=tags_all,
            tags_none=tags_none,
            visibility=visibility,
        )
        return [self._to_record(m) for m in memories]

    async def content_exists(
        self,
        content: str,
        scope: MemoryScope,
        *,
        visibility: Visibility = "active",
    ) -> bool:
        return cast(
            bool,
            await self._run_storage(
                self._storage.content_exists, content, scope, visibility=visibility
            ),
        )

    async def get_memory_by_content(
        self,
        content: str,
        scope: MemoryScope,
        *,
        visibility: Visibility = "active",
    ) -> MemoryRecord | None:
        memory = await self._run_storage(
            self._storage.get_memory_by_content, content, scope, visibility=visibility
        )
        if memory:
            return self._to_record(memory)
        return None

    def _to_record(
        self,
        memory: Any,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        created_at = parse_stored_datetime(memory.created_at) or utc_now()
        updated_at = parse_stored_datetime(memory.updated_at)
        last_accessed = parse_stored_datetime(memory.last_accessed_at)
        deleted_at = parse_stored_datetime(memory.deleted_at)
        last_dreamed_at = parse_stored_datetime(memory.last_dreamed_at)

        return MemoryRecord(
            id=memory.id,
            content=memory.content,
            created_at=created_at,
            memory_type=memory.memory_type,
            updated_at=updated_at,
            project_id=memory.project_id,
            is_global=memory.is_global,
            user_id=user_id,
            tags=memory.tags or [],
            source_type=memory.source_type,
            source_session_id=memory.source_session_id,
            rationale=memory.rationale,
            source_task_id=memory.source_task_id,
            created_by_agent=memory.created_by_agent,
            access_count=memory.access_count,
            last_accessed_at=last_accessed,
            metadata=metadata or {},
            deleted_at=deleted_at,
            dream_action=memory.dream_action,
            last_dreamed_at=last_dreamed_at,
            dream_due_version=memory.dream_due_version,
            vector_needs_reindex=memory.vector_needs_reindex,
        )
