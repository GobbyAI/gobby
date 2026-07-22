"""Tests for memory backend protocol types.

Tests the abstraction layer that enables pluggable memory backends:
- MemoryCapability enum - capabilities that backends can support
- MemoryQuery dataclass - search parameters for recall operations
- MemoryRecord dataclass - backend-agnostic memory representation
- MemoryBackendProtocol - the protocol interface backends must implement

TDD RED phase: These tests define expected behavior before implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

# These imports should fail until protocol.py is implemented
from gobby.memory.protocol import (
    MemoryBackendProtocol,
    MemoryCapability,
    MemoryQuery,
    MemoryRecord,
)
from gobby.memory.write_result import MemoryWriteResult
from gobby.storage.memories import MemoryType
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope

pytestmark = pytest.mark.unit

# =============================================================================
# Test: MemoryCapability Enum
# =============================================================================


class TestMemoryCapability:
    """Tests for MemoryCapability enum."""

    def test_basic_capabilities_exist(self) -> None:
        """Test that core capabilities are defined."""
        # Basic CRUD
        assert MemoryCapability.CREATE is not None
        assert MemoryCapability.READ is not None
        assert MemoryCapability.UPDATE is not None
        assert MemoryCapability.DELETE is not None

    def test_search_capabilities_exist(self) -> None:
        """Test that search-related capabilities are defined."""
        assert MemoryCapability.SEARCH_TEXT is not None
        assert MemoryCapability.SEARCH_SEMANTIC is not None
        assert MemoryCapability.SEARCH_HYBRID is not None

    def test_advanced_capabilities_exist(self) -> None:
        """Test that advanced capabilities are defined."""
        assert MemoryCapability.TAGS is not None
        assert MemoryCapability.CROSSREF is not None

    def test_capability_is_enum_member(self) -> None:
        """Test that capabilities are proper enum members."""
        from enum import Enum

        assert isinstance(MemoryCapability.CREATE, Enum)

    def test_capabilities_are_unique(self) -> None:
        """Test that all capability values are unique."""
        values = [cap.value for cap in MemoryCapability]
        assert len(values) == len(set(values))


# =============================================================================
# Test: MemoryQuery Dataclass
# =============================================================================


class TestMemoryQuery:
    """Tests for MemoryQuery dataclass."""

    def test_create_minimal_query(self) -> None:
        """Test creating a query with just text."""
        query = MemoryQuery(text="search term")
        assert query.text == "search term"

    def test_create_query_with_all_fields(self) -> None:
        """Test creating a query with all parameters."""
        query = MemoryQuery(
            text="search term",
            scope=MemoryScope.project_visible("proj-123"),
            user_id="user-456",
            limit=20,
            memory_type="fact",
            tags_all=["important", "verified"],
            tags_any=["work", "personal"],
            tags_none=["archived"],
            search_mode="semantic",
        )
        assert query.text == "search term"
        assert query.scope == MemoryScope.project_visible("proj-123")
        assert query.user_id == "user-456"
        assert query.limit == 20
        assert query.memory_type == "fact"
        assert query.tags_all == ["important", "verified"]
        assert query.tags_any == ["work", "personal"]
        assert query.tags_none == ["archived"]
        assert query.search_mode == "semantic"

    def test_default_values(self) -> None:
        """Test that optional fields have sensible defaults."""
        query = MemoryQuery(text="test")
        assert query.scope == ALL_MEMORIES
        assert query.user_id is None
        assert query.limit == 10  # Default limit
        assert query.memory_type is None
        assert query.tags_all is None
        assert query.tags_any is None
        assert query.tags_none is None
        assert query.search_mode == "auto"

    def test_memory_type_is_canonical_enum(self) -> None:
        query = MemoryQuery(text="test", memory_type=cast(MemoryType, "pattern"))

        assert query.memory_type is MemoryType.PATTERN

    def test_noncanonical_memory_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid memory_type 'debugging_pattern'"):
            MemoryQuery(
                text="test",
                memory_type=cast(MemoryType, "debugging_pattern"),
            )

    def test_query_is_immutable(self) -> None:
        """Test that query is a frozen dataclass."""
        query = MemoryQuery(text="test")
        with pytest.raises((AttributeError, TypeError)):
            query.text = "modified"


# =============================================================================
# Test: MemoryRecord Dataclass
# =============================================================================


class TestMemoryRecord:
    """Tests for MemoryRecord dataclass."""

    def test_create_minimal_record(self) -> None:
        """Test creating a record with required fields only."""
        record = MemoryRecord(
            id="mem-123",
            content="This is a memory",
            created_at=datetime.now(UTC),
        )
        assert record.id == "mem-123"
        assert record.content == "This is a memory"
        assert record.created_at is not None

    def test_create_full_record(self) -> None:
        """Test creating a record with all fields."""
        now = datetime.now(UTC)
        record = MemoryRecord(
            id="mem-456",
            content="Memory with all fields",
            memory_type="fact",
            created_at=now,
            updated_at=now,
            project_id="proj-789",
            user_id="user-abc",
            tags=["important", "work"],
            source_type="user",
            source_session_id="sess-xyz",
            access_count=5,
            last_accessed_at=now,
            metadata={"custom": "data"},
        )
        assert record.id == "mem-456"
        assert record.memory_type == "fact"
        assert record.tags == ["important", "work"]
        assert record.metadata["custom"] == "data"

    def test_default_values(self) -> None:
        """Test that optional fields have sensible defaults."""
        record = MemoryRecord(
            id="mem-test",
            content="Test content",
            created_at=datetime.now(UTC),
        )
        assert record.memory_type == "fact"  # Default type
        assert record.tags == [] or record.tags is None
        assert record.access_count == 0

    def test_noncanonical_memory_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid memory_type 'debugging_pattern'"):
            MemoryRecord(
                id="mem-invalid",
                content="Invalid",
                created_at=datetime.now(UTC),
                memory_type=cast(MemoryType, "debugging_pattern"),
            )

    def test_record_to_dict(self) -> None:
        """Test converting record to dictionary."""
        now = datetime.now(UTC)
        record = MemoryRecord(
            id="mem-dict",
            content="Dict test",
            created_at=now,
        )
        data = record.to_dict()
        assert data["id"] == "mem-dict"
        assert data["content"] == "Dict test"
        assert "created_at" in data

    def test_record_from_dict(self) -> None:
        """Test creating record from dictionary."""
        data = {
            "id": "mem-from-dict",
            "content": "From dict content",
            "created_at": datetime.now(UTC).isoformat(),
            "memory_type": "preference",
            "project_id": "proj-123",
        }
        record = MemoryRecord.from_dict(data)
        assert record.id == "mem-from-dict"
        assert record.memory_type == "preference"
        assert record.project_id == "proj-123"


# =============================================================================
# Test: MemoryBackendProtocol
# =============================================================================


class TestMemoryBackendProtocol:
    """Tests for MemoryBackendProtocol interface."""

    def test_protocol_is_runtime_checkable(self) -> None:
        """Test that protocol can be checked at runtime."""
        # The protocol should be decorated with @runtime_checkable
        assert hasattr(MemoryBackendProtocol, "__protocol_attrs__") or isinstance(
            MemoryBackendProtocol, type
        )

    def test_protocol_defines_capabilities_method(self) -> None:
        """Test that protocol defines capabilities() method."""
        assert hasattr(MemoryBackendProtocol, "capabilities")

    def test_protocol_defines_create_method(self) -> None:
        """Test that protocol defines create() method."""
        assert hasattr(MemoryBackendProtocol, "create")

    def test_protocol_defines_get_method(self) -> None:
        """Test that protocol defines get() method."""
        assert hasattr(MemoryBackendProtocol, "get")

    def test_protocol_defines_update_method(self) -> None:
        """Test that protocol defines update() method."""
        assert hasattr(MemoryBackendProtocol, "update")

    def test_protocol_defines_delete_method(self) -> None:
        """Test that protocol defines delete() method."""
        assert hasattr(MemoryBackendProtocol, "delete")

    def test_protocol_defines_search_method(self) -> None:
        """Test that protocol defines search() method."""
        assert hasattr(MemoryBackendProtocol, "search")

    def test_protocol_defines_list_method(self) -> None:
        """Test that protocol defines list_memories() method."""
        assert hasattr(MemoryBackendProtocol, "list_memories")


class TestMemoryBackendProtocolCompliance:
    """Tests to verify a mock backend satisfies the protocol."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock that satisfies the protocol."""

        class MockBackend:
            def capabilities(self) -> set[MemoryCapability]:
                return {MemoryCapability.CREATE, MemoryCapability.READ}

            async def create(
                self,
                content: str,
                memory_type: str = "fact",
                project_id: str | None = None,
                user_id: str | None = None,
                tags: list[str] | None = None,
                supersedes: list[str] | None = None,
                source_type: str | None = None,
                source_session_id: str | None = None,
                metadata: dict | None = None,
            ) -> MemoryWriteResult[MemoryRecord]:
                return MemoryWriteResult(
                    MemoryRecord(
                        id="mock-id",
                        content=content,
                        created_at=datetime.now(UTC),
                    ),
                    "created",
                )

            async def get(self, memory_id: str) -> MemoryRecord | None:
                return None

            async def update(
                self,
                memory_id: str,
                content: str | None = None,
                tags: list[str] | None = None,
            ) -> MemoryRecord:
                return MemoryRecord(
                    id=memory_id,
                    content=content or "updated",
                    created_at=datetime.now(UTC),
                )

            async def delete(self, memory_id: str) -> bool:
                return True

            async def search(self, query: MemoryQuery) -> list[MemoryRecord]:
                return []

            async def list_memories(
                self,
                project_id: str | None = None,
                user_id: str | None = None,
                memory_type: str | None = None,
                limit: int = 50,
                offset: int = 0,
            ) -> list[MemoryRecord]:
                return []

            async def content_exists(self, content: str, project_id: str | None = None) -> bool:
                return False

            async def get_memory_by_content(
                self, content: str, project_id: str | None = None
            ) -> MemoryRecord | None:
                return None

        return MockBackend()

    def test_mock_satisfies_protocol(self, mock_backend) -> None:
        """Test that mock backend is recognized as implementing the protocol."""
        assert isinstance(mock_backend, MemoryBackendProtocol)

    @pytest.mark.asyncio
    async def test_create_returns_record(self, mock_backend):
        """Test that create returns a typed MemoryRecord outcome."""
        result = await mock_backend.create("test content")
        assert isinstance(result, MemoryWriteResult)
        assert isinstance(result.memory, MemoryRecord)
        assert result.outcome == "created"
        assert result.memory.content == "test content"

    @pytest.mark.asyncio
    async def test_search_returns_list(self, mock_backend) -> None:
        """Test that search returns a list of records."""
        query = MemoryQuery(text="test")
        results = await mock_backend.search(query)
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_get_returns_optional_record(self, mock_backend) -> None:
        """Test that get returns Optional[MemoryRecord]."""
        record = await mock_backend.get("mock-id")
        assert record is None

    @pytest.mark.asyncio
    async def test_update_returns_record(self, mock_backend) -> None:
        """Test that update returns MemoryRecord."""
        record = await mock_backend.update("mock-id", content="updated")
        assert isinstance(record, MemoryRecord)
        assert record.content == "updated"

    @pytest.mark.asyncio
    async def test_delete_returns_bool(self, mock_backend) -> None:
        """Test that delete returns boolean."""
        result = await mock_backend.delete("mock-id")
        assert isinstance(result, bool)
        assert result is True

    @pytest.mark.asyncio
    async def test_list_memories_returns_list(self, mock_backend) -> None:
        """Test that list_memories returns list[MemoryRecord]."""
        records = await mock_backend.list_memories()
        assert isinstance(records, list)

    def test_capabilities_returns_set(self, mock_backend) -> None:
        """Test that capabilities returns a set of MemoryCapability."""
        caps = mock_backend.capabilities()
        assert isinstance(caps, set)
        assert all(isinstance(cap, MemoryCapability) for cap in caps)


# =============================================================================
# Test: Module Exports
# =============================================================================


class TestModuleExports:
    """Tests for module __all__ exports."""

    def test_all_types_exported(self) -> None:
        """Test that all public types are in __all__."""
        from gobby.memory import protocol

        assert "MemoryCapability" in protocol.__all__
        assert "MemoryQuery" in protocol.__all__
        assert "MemoryRecord" in protocol.__all__
        assert "MemoryBackendProtocol" in protocol.__all__
