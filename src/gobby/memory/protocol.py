"""Memory backend protocol types.

This module defines the abstraction layer that enables pluggable memory backends.
Users can choose Gobby's built-in PostgreSQL hub backend or plug in external
memory systems.

Types:
- MemoryCapability: Enum of capabilities a backend can support
- MemoryQuery: Dataclass for search parameters
- MemoryRecord: Backend-agnostic memory representation
- MemoryBackendProtocol: Protocol interface that backends must implement

Example:
    from gobby.memory.protocol import MemoryBackendProtocol, MemoryCapability

    class MyBackend:
        def capabilities(self) -> set[MemoryCapability]:
            return {MemoryCapability.CREATE, MemoryCapability.READ}
        # ... implement other required methods

    assert isinstance(MyBackend(), MemoryBackendProtocol)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from gobby.storage.memories import Visibility

__all__ = [
    "MemoryCapability",
    "MemoryQuery",
    "MemoryRecord",
    "MemoryBackendProtocol",
]


class MemoryCapability(Enum):
    """Capabilities that a memory backend can support.

    Backends declare which capabilities they support via the capabilities()
    method. The MemoryManager uses these to gracefully degrade when a backend
    doesn't support a requested operation.

    Basic CRUD:
        CREATE: Store new memories
        READ: Retrieve a specific memory by ID
        UPDATE: Modify existing memories
        DELETE: Remove memories

    Search capabilities:
        SEARCH_TEXT: Text-based substring/keyword search
        SEARCH_SEMANTIC: Embedding-based semantic similarity search
        SEARCH_HYBRID: Combined text + semantic search

    Advanced features:
        TAGS: Tag-based filtering and organization
        CROSSREF: Cross-referencing between related memories

    MCP-aligned operations (aliases for compatibility):
        REMEMBER: Alias for CREATE
        RECALL: Alias for READ + SEARCH
        FORGET: Alias for DELETE
        SEARCH: Generic search (text or semantic)
        LIST: List/enumerate memories
        EXISTS: Check if memory exists
        STATS: Get statistics about memories
    """

    # Basic CRUD
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"

    # Search capabilities
    SEARCH_TEXT = "search_text"
    SEARCH_SEMANTIC = "search_semantic"
    SEARCH_HYBRID = "search_hybrid"

    # Advanced features
    TAGS = "tags"
    CROSSREF = "crossref"

    # MCP-aligned operations (aliases)
    REMEMBER = "remember"
    RECALL = "recall"
    FORGET = "forget"
    SEARCH = "search"
    LIST = "list"
    EXISTS = "exists"
    STATS = "stats"


@dataclass(frozen=True)
class MemoryQuery:
    """Search parameters for memory recall operations.

    Attributes:
        text: Search query text (required for search operations)
        project_id: Filter by project ID
        user_id: Filter by user ID (for multi-tenant backends)
        limit: Maximum number of results to return
        memory_type: Filter by memory type (fact, preference, etc.)
        tags_all: Memory must have ALL of these tags
        tags_any: Memory must have at least ONE of these tags
        tags_none: Memory must have NONE of these tags
        include_global: Include global memories when project_id is provided
        search_mode: Search mode - "auto", "text", "semantic", "hybrid"

    Example:
        query = MemoryQuery(
            text="authentication",
            project_id="proj-123",
            tags_all=["security"],
            search_mode="semantic"
        )
    """

    text: str
    project_id: str | None = None
    user_id: str | None = None
    limit: int = 10
    memory_type: str | None = None
    tags_all: list[str] | None = None
    tags_any: list[str] | None = None
    tags_none: list[str] | None = None
    include_global: bool = True
    search_mode: str = "auto"
    visibility: Visibility = "active"


@dataclass
class MemoryRecord:
    """Backend-agnostic representation of a memory.

    This is the common format used across all backends. Backends convert
    their internal representations to/from this format.

    Attributes:
        id: Unique identifier for the memory
        content: The memory content text
        created_at: When the memory was created
        memory_type: Type of memory (fact, preference, pattern, context)
        updated_at: When the memory was last updated
        project_id: Associated project ID
        user_id: Associated user ID (for multi-tenant backends)
        tags: List of tags for organization
        source_type: Origin of memory — "user" (human-requested) or "agent" (agent-captured)
        source_session_id: Session that created the memory
        access_count: Number of times memory was accessed
        last_accessed_at: When memory was last accessed
        metadata: Additional backend-specific metadata

    Example:
        record = MemoryRecord(
            id="mem-abc123",
            content="User prefers dark mode",
            created_at=datetime.now(UTC),
            memory_type="preference",
            tags=["ui", "settings"]
        )
    """

    id: str
    content: str
    created_at: datetime
    memory_type: str = "fact"
    updated_at: datetime | None = None
    project_id: str | None = None
    user_id: str | None = None
    tags: list[str] = field(default_factory=list)
    source_type: str = "agent"
    source_session_id: str | None = None
    access_count: int = 0
    last_accessed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    deleted_at: datetime | None = None
    dream_action: str | None = None
    last_dreamed_at: datetime | None = None
    vector_needs_reindex: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert record to dictionary for serialization."""
        return {
            "id": self.id,
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "memory_type": self.memory_type,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "project_id": self.project_id,
            "user_id": self.user_id,
            "tags": self.tags,
            "source_type": self.source_type,
            "source_session_id": self.source_session_id,
            "access_count": self.access_count,
            "last_accessed_at": (
                self.last_accessed_at.isoformat() if self.last_accessed_at else None
            ),
            "metadata": self.metadata,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "dream_action": self.dream_action,
            "last_dreamed_at": (self.last_dreamed_at.isoformat() if self.last_dreamed_at else None),
            "vector_needs_reindex": self.vector_needs_reindex,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryRecord:
        """Create record from dictionary."""
        # Parse datetime fields
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now(UTC)

        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)

        last_accessed_at = data.get("last_accessed_at")
        if isinstance(last_accessed_at, str):
            last_accessed_at = datetime.fromisoformat(last_accessed_at)

        deleted_at = data.get("deleted_at")
        if isinstance(deleted_at, str):
            deleted_at = datetime.fromisoformat(deleted_at)

        last_dreamed_at = data.get("last_dreamed_at")
        if isinstance(last_dreamed_at, str):
            last_dreamed_at = datetime.fromisoformat(last_dreamed_at)

        return cls(
            id=data["id"],
            content=data["content"],
            created_at=created_at,
            memory_type=data.get("memory_type", "fact"),
            updated_at=updated_at,
            project_id=data.get("project_id"),
            user_id=data.get("user_id"),
            tags=data.get("tags", []),
            source_type=data.get("source_type", "agent"),
            source_session_id=data.get("source_session_id"),
            access_count=data.get("access_count", 0),
            last_accessed_at=last_accessed_at,
            metadata=data.get("metadata", {}),
            deleted_at=deleted_at,
            dream_action=data.get("dream_action"),
            last_dreamed_at=last_dreamed_at,
            vector_needs_reindex=bool(data.get("vector_needs_reindex", False)),
        )


@runtime_checkable
class MemoryBackendProtocol(Protocol):
    """Protocol interface that memory backends must implement.

    Backends can implement a subset of methods based on their capabilities.
    The capabilities() method declares which operations the backend supports,
    allowing the MemoryManager to gracefully degrade for unsupported operations.

    Required methods:
        capabilities(): Return set of supported MemoryCapability values
        create(): Store a new memory
        get(): Retrieve a memory by ID
        update(): Update an existing memory
        delete(): Delete a memory
        search(): Search for memories
        list_memories(): List memories with filtering

    Example:
        class MyBackend:
            def capabilities(self) -> set[MemoryCapability]:
                return {MemoryCapability.CREATE, MemoryCapability.READ}

            async def create(self, content: str, **kwargs) -> MemoryRecord:
                # Implementation...

        backend = MyBackend()
        assert isinstance(backend, MemoryBackendProtocol)
    """

    def capabilities(self) -> set[MemoryCapability]:
        """Return the set of capabilities this backend supports."""
        ...

    async def create(
        self,
        content: str,
        memory_type: str = "fact",
        project_id: str | None = None,
        user_id: str | None = None,
        tags: list[str] | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        """Create a new memory.

        Args:
            content: The memory content text
            memory_type: Type of memory (fact, preference, etc.)
            project_id: Associated project ID
            user_id: Associated user ID
            tags: List of tags
            source_type: Origin of memory
            source_session_id: Session that created the memory
            metadata: Additional metadata

        Returns:
            The created MemoryRecord
        """
        ...

    async def get(
        self, memory_id: str, *, visibility: Visibility = "active"
    ) -> MemoryRecord | None:
        """Retrieve a memory by ID.

        Args:
            memory_id: The memory ID to retrieve

        Returns:
            The MemoryRecord if found, None otherwise
        """
        ...

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """Update an existing memory.

        Args:
            memory_id: The memory ID to update
            content: New content (optional)
            tags: New tags (optional)

        Returns:
            The updated MemoryRecord

        Raises:
            ValueError: If memory not found
        """
        ...

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory.

        Args:
            memory_id: The memory ID to delete

        Returns:
            True if deleted, False if not found
        """
        ...

    async def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Search for memories.

        Args:
            query: Search parameters

        Returns:
            List of matching MemoryRecords
        """
        ...

    async def list_memories(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tags_all: list[str] | None = None,
        *,
        visibility: Visibility = "active",
        include_global: bool = True,
    ) -> list[MemoryRecord]:
        """List memories with optional filtering.

        Args:
            project_id: Filter by project ID
            user_id: Filter by user ID
            memory_type: Filter by memory type
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of MemoryRecords
        """
        ...

    async def content_exists(
        self, content: str, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> bool:
        """Check if a memory with identical content already exists.

        Args:
            content: The content to check for
            project_id: Project scope plus visible globals. ``None`` checks globals only.

        Returns:
            True if a memory with identical content exists
        """
        ...

    async def get_memory_by_content(
        self, content: str, project_id: str | None = None, *, visibility: Visibility = "active"
    ) -> MemoryRecord | None:
        """Get a memory by its exact content.

        Args:
            content: The exact content to look up
            project_id: Project scope plus visible globals. ``None`` checks globals only.

        Returns:
            The MemoryRecord if found, None otherwise
        """
        ...
