"""Null memory backend for testing.

This backend provides a no-op implementation that satisfies the protocol
but doesn't persist any data. Useful for:
- Unit tests that don't need real storage
- Integration tests with isolated memory
- Dry-run scenarios
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from gobby.memory.protocol import (
    MediaAttachment,
    MemoryCapability,
    MemoryQuery,
    MemoryRecord,
)


class NullBackend:
    """A no-op memory backend for testing.

    Creates memories in-memory but doesn't persist them.
    Searches always return empty results.
    """

    def capabilities(self) -> set[MemoryCapability]:
        """Return supported capabilities."""
        return {
            MemoryCapability.CREATE,
            MemoryCapability.READ,
            MemoryCapability.UPDATE,
            MemoryCapability.DELETE,
            MemoryCapability.SEARCH,
            MemoryCapability.LIST,
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
        """Create a memory record (in-memory only, not persisted)."""
        now = datetime.now(UTC)
        return MemoryRecord(
            id=f"null-{uuid4().hex[:8]}",
            content=content,
            created_at=now,
            memory_type=memory_type,
            project_id=project_id,
            user_id=user_id,
            tags=tags or [],
            source_type=source_type,
            source_session_id=source_session_id,
            media=media or [],
            metadata=metadata or {},
        )

    async def get(self, memory_id: str) -> MemoryRecord | None:
        """Get a memory by ID (always returns None - no persistence)."""
        return None

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """Update a memory (creates a new record since nothing is persisted)."""
        now = datetime.now(UTC)
        return MemoryRecord(
            id=memory_id,
            content=content or "",
            created_at=now,
            updated_at=now,
            tags=tags or [],
        )

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory (always returns False - nothing to delete)."""
        return False

    async def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Search for memories (always returns empty list)."""
        return []

    async def list_memories(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        """List memories (always returns empty list)."""
        return []

    async def content_exists(self, content: str, project_id: str | None = None) -> bool:
        """Check if content exists (always returns False)."""
        return False

    async def get_memory_by_content(
        self, content: str, project_id: str | None = None
    ) -> MemoryRecord | None:
        """Get memory by content (always returns None)."""
        return None
