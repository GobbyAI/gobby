"""Null memory backend for testing.

This backend provides a no-op implementation that satisfies the protocol
but doesn't persist any data. Useful for:
- Unit tests that don't need real storage
- Integration tests with isolated memory
- Dry-run scenarios
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gobby.memory.protocol import MemoryCapability, MemoryQuery, MemoryRecord
from gobby.memory.write_result import MemoryWriteResult
from gobby.storage.memories_models import validate_memory_type
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope
from gobby.storage.projects import PERSONAL_PROJECT_ID

if TYPE_CHECKING:
    from gobby.storage.memories import Visibility


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
        """Create a memory record (in-memory only, not persisted)."""
        now = datetime.now(UTC)
        record = MemoryRecord(
            id=f"null-{uuid4().hex[:8]}",
            content=content,
            created_at=now,
            memory_type=validate_memory_type(memory_type),
            project_id=project_id,
            is_global=is_global,
            user_id=user_id,
            tags=list(
                dict.fromkeys([*(tags or []), *(f"supersedes:{item}" for item in supersedes or [])])
            ),
            source_type=source_type,
            source_session_id=source_session_id,
            rationale=rationale,
            source_task_id=source_task_id,
            created_by_agent=created_by_agent,
            metadata=metadata or {},
        )
        return MemoryWriteResult(record, "created")

    async def get(
        self, memory_id: str, *, visibility: Visibility = "active"
    ) -> MemoryRecord | None:
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
            project_id=PERSONAL_PROJECT_ID,
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
        scope: MemoryScope = ALL_MEMORIES,
        user_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        *,
        visibility: Visibility = "active",
    ) -> list[MemoryRecord]:
        """List memories (always returns empty list)."""
        return []

    async def content_exists(
        self,
        content: str,
        scope: MemoryScope,
        *,
        visibility: Visibility = "active",
    ) -> bool:
        """Check if content exists (always returns False)."""
        return False

    async def get_memory_by_content(
        self,
        content: str,
        scope: MemoryScope,
        *,
        visibility: Visibility = "active",
    ) -> MemoryRecord | None:
        """Get memory by content (always returns None)."""
        return None
