"""Storage adapter for MemoryBackendProtocol.

Wraps an existing LocalMemoryManager instance to provide the async
MemoryBackendProtocol interface. Used by MemoryManager when operating
in local/SQLite mode (the default).

Unlike the old SQLiteBackend (deleted in Memory V4), this adapter does NOT
create its own LocalMemoryManager — it reuses the one owned by MemoryManager,
eliminating the duplicate-instance problem.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from gobby.memory.protocol import (
    MediaAttachment,
    MemoryCapability,
    MemoryQuery,
    MemoryRecord,
)
from gobby.storage.memories import LocalMemoryManager


class StorageAdapter:
    """Adapts LocalMemoryManager to the async MemoryBackendProtocol interface."""

    def __init__(self, storage: LocalMemoryManager):
        self._storage = storage

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
        memory_type: str = "fact",
        project_id: str | None = None,
        user_id: str | None = None,
        tags: list[str] | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        media: list[MediaAttachment] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        media_json: str | None = None
        if media:
            media_json = json.dumps(
                [
                    {
                        "media_type": m.media_type,
                        "content_path": m.content_path,
                        "mime_type": m.mime_type,
                        "description": m.description,
                        "description_model": m.description_model,
                        "metadata": m.metadata,
                    }
                    for m in media
                ]
            )

        memory = await asyncio.to_thread(
            self._storage.create_memory,
            content=content,
            memory_type=memory_type,
            project_id=project_id,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
            media=media_json,
        )
        return self._to_record(memory, user_id=user_id, metadata=metadata)

    async def get(self, memory_id: str) -> MemoryRecord | None:
        try:
            memory = await asyncio.to_thread(self._storage.get_memory, memory_id)
            return self._to_record(memory)
        except ValueError:
            return None

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        memory = await asyncio.to_thread(
            self._storage.update_memory,
            memory_id=memory_id,
            content=content,
            tags=tags,
        )
        if memory is None:
            raise ValueError(f"Memory not found: {memory_id}")
        return self._to_record(memory)

    async def delete(self, memory_id: str) -> bool:
        return await asyncio.to_thread(self._storage.delete_memory, memory_id)

    async def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        memories = await asyncio.to_thread(
            self._storage.search_memories,
            query_text=query.text,
            project_id=query.project_id,
            limit=query.limit,
            tags_all=query.tags_all,
            tags_any=query.tags_any,
            tags_none=query.tags_none,
        )
        if query.memory_type is not None:
            memories = [m for m in memories if m.memory_type == query.memory_type]
        return [self._to_record(m) for m in memories]

    async def list_memories(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        memories = await asyncio.to_thread(
            self._storage.list_memories,
            project_id=project_id,
            memory_type=memory_type,
            limit=limit,
            offset=offset,
        )
        return [self._to_record(m) for m in memories]

    async def content_exists(self, content: str, project_id: str | None = None) -> bool:
        return await asyncio.to_thread(self._storage.content_exists, content, project_id)

    async def get_memory_by_content(
        self, content: str, project_id: str | None = None
    ) -> MemoryRecord | None:
        memory = await asyncio.to_thread(self._storage.get_memory_by_content, content, project_id)
        if memory:
            return self._to_record(memory)
        return None

    def _to_record(
        self,
        memory: Any,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        created_at = (
            datetime.fromisoformat(memory.created_at) if memory.created_at else datetime.now(UTC)
        )
        updated_at = datetime.fromisoformat(memory.updated_at) if memory.updated_at else None
        last_accessed = (
            datetime.fromisoformat(memory.last_accessed_at) if memory.last_accessed_at else None
        )

        media_list: list[MediaAttachment] = []
        if memory.media:
            try:
                media_data = json.loads(memory.media)
                media_list = [
                    MediaAttachment(
                        media_type=m.get("media_type", "unknown"),
                        content_path=m.get("content_path", ""),
                        mime_type=m.get("mime_type", "application/octet-stream"),
                        description=m.get("description"),
                        description_model=m.get("description_model"),
                        metadata=m.get("metadata"),
                    )
                    for m in media_data
                ]
            except (json.JSONDecodeError, TypeError):
                media_list = []

        return MemoryRecord(
            id=memory.id,
            content=memory.content,
            created_at=created_at,
            memory_type=memory.memory_type,
            updated_at=updated_at,
            project_id=memory.project_id,
            user_id=user_id,
            tags=memory.tags or [],
            source_type=memory.source_type,
            source_session_id=memory.source_session_id,
            access_count=memory.access_count,
            last_accessed_at=last_accessed,
            media=media_list,
            metadata=metadata or {},
        )
