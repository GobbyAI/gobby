"""
Tests for gobby.mcp_proxy.tools.memory module.

Tests the memory MCP tools including:
- create_memory
- search_memories
- delete_memory
- list_memories
- get_memory
- get_related_memories
- update_memory
- remember_with_image
- remember_screenshot
- memory_stats
- search_knowledge_graph
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.memory import create_memory_registry, get_current_project_id

pytestmark = pytest.mark.unit


class MockMemory:
    """Mock memory object for tests."""

    def __init__(
        self,
        id: str = "mem-123",
        content: str = "Test memory content",
        memory_type: str = "fact",
        created_at: str = "2024-01-01T00:00:00",
        updated_at: str | None = None,
        project_id: str | None = None,
        source_type: str = "mcp_tool",
        source_session_id: str | None = None,
        access_count: int = 0,
        tags: list[str] | None = None,
        similarity: float | None = None,
    ):
        self.id = id
        self.content = content
        self.memory_type = memory_type
        self.created_at = created_at
        self.updated_at = updated_at or created_at
        self.project_id = project_id
        self.source_type = source_type
        self.source_session_id = source_session_id
        self.access_count = access_count
        self.tags = tags or []
        if similarity is not None:
            self.similarity = similarity


@pytest.fixture
def mock_memory_manager():
    """Create a mock memory manager."""
    manager = MagicMock()
    manager.create_memory = AsyncMock(return_value=MockMemory())
    manager.search_memories = AsyncMock(return_value=[MockMemory()])
    manager.delete_memory = AsyncMock(return_value=True)
    manager.list_memories = MagicMock(return_value=[MockMemory()])
    manager.get_memory = MagicMock(return_value=MockMemory())
    manager.get_related = AsyncMock(return_value=[MockMemory()])
    manager.update_memory = AsyncMock(return_value=MockMemory())
    manager.get_stats = MagicMock(return_value={"total": 10, "by_type": {"fact": 5}})
    manager.db = MagicMock()
    manager.content_exists = MagicMock(return_value=False)
    return manager


@pytest.fixture
def memory_registry(mock_memory_manager):
    """Create a memory registry with mocked dependencies."""
    return create_memory_registry(mock_memory_manager)


class TestGetCurrentProjectId:
    """Tests for get_current_project_id helper."""

    def test_returns_project_id(self) -> None:
        """Returns project ID when available."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "project-123", "name": "test"}
            result = get_current_project_id()
            assert result == "project-123"

    def test_returns_none_when_no_context(self) -> None:
        """Returns None when no project context."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = None
            result = get_current_project_id()
            assert result is None

    def test_returns_none_when_no_id(self) -> None:
        """Returns None when context has no ID."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"name": "test"}
            result = get_current_project_id()
            assert result is None


class TestCreateMemory:
    """Tests for create_memory tool."""

    @pytest.mark.asyncio
    async def test_create_memory_success(self, memory_registry, mock_memory_manager):
        """Test successful memory creation with similar_existing in response."""
        mock_memory_manager.search_memories.return_value = [
            MockMemory(id="existing-1", content="Similar memory", similarity=0.85),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context", return_value={"id": "proj-1"}
        ):
            result = await memory_registry.call(
                "create_memory",
                {"content": "Test content", "memory_type": "fact"},
            )

        assert result["success"] is True
        assert "memory" in result
        assert result["memory"]["id"] == "mem-123"
        assert "similar_existing" in result
        assert len(result["similar_existing"]) == 1
        assert result["similar_existing"][0]["id"] == "existing-1"
        assert result["similar_existing"][0]["similarity"] == 0.85
        mock_memory_manager.create_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_memory_with_tags(self, memory_registry, mock_memory_manager):
        """Test memory creation with tags."""
        mock_memory_manager.search_memories.return_value = []

        with patch(
            "gobby.utils.project_context.get_project_context", return_value={"id": "proj-1"}
        ):
            result = await memory_registry.call(
                "create_memory", {"content": "Test", "tags": ["tag1", "tag2"]}
            )

        assert result["success"] is True
        assert result["similar_existing"] == []
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["tags"] == ["tag1", "tag2"]

    @pytest.mark.asyncio
    async def test_create_memory_with_session_id(self, memory_registry, mock_memory_manager):
        """Test memory creation resolves and passes source_session_id."""
        mock_memory_manager.search_memories.return_value = []
        resolved_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.storage.session_resolution.resolve_session_reference",
                return_value=resolved_uuid,
            ) as mock_resolve,
        ):
            result = await memory_registry.call(
                "create_memory", {"content": "Test", "session_id": "#42"}
            )

        assert result["success"] is True
        mock_resolve.assert_called_once_with(mock_memory_manager.db, "#42", "proj-1")
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["source_session_id"] == resolved_uuid

    @pytest.mark.asyncio
    async def test_create_memory_session_resolution_failure(
        self, memory_registry, mock_memory_manager
    ):
        """Test memory creation falls back to None when session resolution fails."""
        mock_memory_manager.search_memories.return_value = []

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "proj-1"},
            ),
            patch(
                "gobby.storage.session_resolution.resolve_session_reference",
                side_effect=ValueError("Session not found"),
            ),
        ):
            result = await memory_registry.call(
                "create_memory", {"content": "Test", "session_id": "#999"}
            )

        assert result["success"] is True
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["source_session_id"] is None

    @pytest.mark.asyncio
    async def test_create_memory_excludes_self_from_similar(
        self, memory_registry, mock_memory_manager
    ):
        """Test that the newly created memory is excluded from similar_existing."""
        # Recall returns the new memory itself plus another
        mock_memory_manager.search_memories.return_value = [
            MockMemory(id="mem-123", content="Test content", similarity=1.0),
            MockMemory(id="existing-1", content="Similar", similarity=0.9),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context", return_value={"id": "proj-1"}
        ):
            result = await memory_registry.call("create_memory", {"content": "Test content"})

        assert result["success"] is True
        assert len(result["similar_existing"]) == 1
        assert result["similar_existing"][0]["id"] == "existing-1"

    @pytest.mark.asyncio
    async def test_create_memory_similar_search_failure_nonfatal(
        self, memory_registry, mock_memory_manager
    ):
        """Test that similarity search failure doesn't break memory creation."""
        mock_memory_manager.search_memories.side_effect = Exception("Search unavailable")

        with patch(
            "gobby.utils.project_context.get_project_context", return_value={"id": "proj-1"}
        ):
            result = await memory_registry.call("create_memory", {"content": "Test"})

        assert result["success"] is True
        assert result["similar_existing"] == []

    @pytest.mark.asyncio
    async def test_create_memory_error(self, memory_registry, mock_memory_manager):
        """Test memory creation error handling."""
        mock_memory_manager.create_memory.side_effect = Exception("Database error")

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await memory_registry.call("create_memory", {"content": "Test"})

        assert result["success"] is False
        assert "Database error" in result["error"]


class TestSearchMemories:
    """Tests for search_memories tool."""

    @pytest.mark.asyncio
    async def test_search_memories_success(self, memory_registry, mock_memory_manager):
        """Test successful memory search."""
        mock_memory_manager.search_memories.return_value = [
            MockMemory(id="m1", content="Memory 1", similarity=0.95),
            MockMemory(id="m2", content="Memory 2", similarity=0.85),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context", return_value={"id": "proj-1"}
        ):
            result = await memory_registry.call(
                "search_memories", {"query": "test query", "limit": 5}
            )

        assert result["success"] is True
        assert len(result["memories"]) == 2
        assert result["memories"][0]["similarity"] == 0.95

    @pytest.mark.asyncio
    async def test_search_memories_with_filters(self, memory_registry, mock_memory_manager):
        """Test search with tag filters."""
        with patch(
            "gobby.utils.project_context.get_project_context", return_value={"id": "proj-1"}
        ):
            result = await memory_registry.call(
                "search_memories",
                {
                    "query": "test",
                    "tags_all": ["important"],
                    "tags_any": ["work", "personal"],
                    "tags_none": ["archived"],
                },
            )

        assert result["success"] is True
        call_kwargs = mock_memory_manager.search_memories.call_args.kwargs
        assert call_kwargs["tags_all"] == ["important"]
        assert call_kwargs["tags_any"] == ["work", "personal"]
        assert call_kwargs["tags_none"] == ["archived"]

    @pytest.mark.asyncio
    async def test_search_memories_error(self, memory_registry, mock_memory_manager):
        """Test search error handling."""
        mock_memory_manager.search_memories.side_effect = Exception("Search error")

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await memory_registry.call("search_memories", {"query": "test"})

        assert result["success"] is False
        assert "Search error" in result["error"]


class TestDeleteMemory:
    """Tests for delete_memory tool."""

    @pytest.mark.asyncio
    async def test_delete_memory_success(self, memory_registry, mock_memory_manager):
        """Test successful memory deletion."""
        result = await memory_registry.call("delete_memory", {"memory_id": "mem-123"})

        assert result == {"success": True}  # Success response
        mock_memory_manager.delete_memory.assert_called_once_with("mem-123")

    @pytest.mark.asyncio
    async def test_delete_memory_not_found(self, memory_registry, mock_memory_manager):
        """Test deletion when memory not found."""
        mock_memory_manager.delete_memory.return_value = False

        result = await memory_registry.call("delete_memory", {"memory_id": "nonexistent"})

        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_delete_memory_error(self, memory_registry, mock_memory_manager):
        """Test deletion error handling."""
        mock_memory_manager.delete_memory.side_effect = Exception("Delete error")

        result = await memory_registry.call("delete_memory", {"memory_id": "mem-123"})

        assert "error" in result
        assert "Delete error" in result["error"]


class TestListMemories:
    """Tests for list_memories tool."""

    @pytest.mark.asyncio
    async def test_list_memories_success(self, memory_registry, mock_memory_manager):
        """Test successful memory listing."""
        mock_memory_manager.list_memories.return_value = [
            MockMemory(id="m1"),
            MockMemory(id="m2"),
            MockMemory(id="m3"),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context", return_value={"id": "proj-1"}
        ):
            result = await memory_registry.call("list_memories", {})

        assert result["success"] is True
        assert result["count"] == 3
        assert len(result["memories"]) == 3

    @pytest.mark.asyncio
    async def test_list_memories_with_filters(self, memory_registry, mock_memory_manager):
        """Test listing with filters."""
        with patch(
            "gobby.utils.project_context.get_project_context", return_value={"id": "proj-1"}
        ):
            result = await memory_registry.call(
                "list_memories",
                {
                    "memory_type": "fact",
                    "limit": 20,
                    "tags_all": ["work"],
                },
            )

        assert result["success"] is True
        call_kwargs = mock_memory_manager.list_memories.call_args.kwargs
        assert call_kwargs["memory_type"] == "fact"
        assert call_kwargs["limit"] == 20
        assert call_kwargs["tags_all"] == ["work"]

    @pytest.mark.asyncio
    async def test_list_memories_error(self, memory_registry, mock_memory_manager):
        """Test list error handling."""
        mock_memory_manager.list_memories.side_effect = Exception("List error")

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await memory_registry.call("list_memories", {})

        assert result["success"] is False
        assert "List error" in result["error"]


class TestGetMemory:
    """Tests for get_memory tool."""

    @pytest.mark.asyncio
    async def test_get_memory_success(self, memory_registry, mock_memory_manager):
        """Test successful memory retrieval."""
        mock_memory_manager.get_memory.return_value = MockMemory(
            id="mem-123",
            content="Test content",
            memory_type="fact",
            project_id="proj-1",
            access_count=5,
            tags=["tag1"],
        )

        result = await memory_registry.call("get_memory", {"memory_id": "mem-123"})

        assert result["success"] is True
        assert result["memory"]["id"] == "mem-123"
        assert result["memory"]["content"] == "Test content"
        assert result["memory"]["access_count"] == 5
        assert result["memory"]["tags"] == ["tag1"]

    @pytest.mark.asyncio
    async def test_get_memory_not_found(self, memory_registry, mock_memory_manager):
        """Test retrieval when memory not found."""
        mock_memory_manager.get_memory.return_value = None

        result = await memory_registry.call("get_memory", {"memory_id": "nonexistent"})

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_memory_value_error(self, memory_registry, mock_memory_manager):
        """Test retrieval with ValueError."""
        mock_memory_manager.get_memory.side_effect = ValueError("Invalid ID format")

        result = await memory_registry.call("get_memory", {"memory_id": "invalid"})

        assert result["success"] is False
        assert "Invalid ID format" in result["error"]

    @pytest.mark.asyncio
    async def test_get_memory_error(self, memory_registry, mock_memory_manager):
        """Test retrieval error handling."""
        mock_memory_manager.get_memory.side_effect = Exception("Get error")

        result = await memory_registry.call("get_memory", {"memory_id": "mem-123"})

        assert result["success"] is False
        assert "Get error" in result["error"]


class TestGetRelatedMemories:
    """Tests for get_related_memories tool."""

    @pytest.mark.asyncio
    async def test_get_related_memories_success(self, memory_registry, mock_memory_manager):
        """Test successful related memories retrieval."""
        mock_memory_manager.get_related.return_value = [
            MockMemory(id="related-1"),
            MockMemory(id="related-2"),
        ]

        result = await memory_registry.call(
            "get_related_memories", {"memory_id": "mem-123", "limit": 5, "min_similarity": 0.3}
        )

        assert result["success"] is True
        assert result["memory_id"] == "mem-123"
        assert result["count"] == 2
        assert len(result["related"]) == 2
        mock_memory_manager.get_related.assert_called_once_with(
            memory_id="mem-123",
            limit=5,
            min_similarity=0.3,
        )

    @pytest.mark.asyncio
    async def test_get_related_memories_value_error(self, memory_registry, mock_memory_manager):
        """Test related memories with ValueError."""
        mock_memory_manager.get_related.side_effect = ValueError("Memory not found")

        result = await memory_registry.call("get_related_memories", {"memory_id": "nonexistent"})

        assert result["success"] is False
        assert "Memory not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_related_memories_error(self, memory_registry, mock_memory_manager):
        """Test related memories error handling."""
        mock_memory_manager.get_related.side_effect = Exception("Related error")

        result = await memory_registry.call("get_related_memories", {"memory_id": "mem-123"})

        assert result["success"] is False
        assert "Related error" in result["error"]


class TestUpdateMemory:
    """Tests for update_memory tool."""

    @pytest.mark.asyncio
    async def test_update_memory_success(self, memory_registry, mock_memory_manager):
        """Test successful memory update."""
        updated_memory = MockMemory(id="mem-123", updated_at="2024-01-02T00:00:00")
        mock_memory_manager.update_memory.return_value = updated_memory

        result = await memory_registry.call(
            "update_memory",
            {
                "memory_id": "mem-123",
                "content": "Updated content",
                "tags": ["new-tag"],
            },
        )

        assert result["success"] is True
        assert result["memory"]["id"] == "mem-123"
        mock_memory_manager.update_memory.assert_called_once_with(
            memory_id="mem-123",
            content="Updated content",
            tags=["new-tag"],
        )

    @pytest.mark.asyncio
    async def test_update_memory_partial(self, memory_registry, mock_memory_manager):
        """Test partial memory update."""
        mock_memory_manager.update_memory.return_value = MockMemory()

        result = await memory_registry.call(
            "update_memory", {"memory_id": "mem-123", "tags": ["updated"]}
        )

        assert result["success"] is True
        mock_memory_manager.update_memory.assert_called_once_with(
            memory_id="mem-123",
            content=None,
            tags=["updated"],
        )

    @pytest.mark.asyncio
    async def test_update_memory_value_error(self, memory_registry, mock_memory_manager):
        """Test update with ValueError."""
        mock_memory_manager.update_memory.side_effect = ValueError("Memory not found")

        result = await memory_registry.call(
            "update_memory", {"memory_id": "nonexistent", "content": "New"}
        )

        assert result["success"] is False
        assert "Memory not found" in result["error"]

    @pytest.mark.asyncio
    async def test_update_memory_error(self, memory_registry, mock_memory_manager):
        """Test update error handling."""
        mock_memory_manager.update_memory.side_effect = Exception("Update error")

        result = await memory_registry.call(
            "update_memory", {"memory_id": "mem-123", "content": "New"}
        )

        assert result["success"] is False
        assert "Update error" in result["error"]


class TestMemoryStats:
    """Tests for memory_stats tool."""

    @pytest.mark.asyncio
    async def test_memory_stats_success(self, memory_registry, mock_memory_manager):
        """Test successful stats retrieval."""
        mock_memory_manager.get_stats.return_value = {
            "total": 100,
            "by_type": {"fact": 60, "preference": 40},
        }

        with patch(
            "gobby.utils.project_context.get_project_context", return_value={"id": "proj-1"}
        ):
            result = await memory_registry.call("memory_stats", {})

        assert "stats" in result
        assert result["stats"]["total"] == 100
        mock_memory_manager.get_stats.assert_called_once_with(project_id="proj-1")

    @pytest.mark.asyncio
    async def test_memory_stats_error(self, memory_registry, mock_memory_manager):
        """Test stats error handling."""
        mock_memory_manager.get_stats.side_effect = Exception("Stats error")

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await memory_registry.call("memory_stats", {})

        assert "error" in result
        assert "Stats error" in result["error"]


class TestRegistryCreation:
    """Tests for create_memory_registry function."""

    def test_creates_registry(self, mock_memory_manager) -> None:
        """Test registry is created with correct name."""
        registry = create_memory_registry(mock_memory_manager)

        assert registry.name == "gobby-memory"
        assert "memory management" in registry.description.lower()

    def test_all_tools_registered(self, mock_memory_manager) -> None:
        """Test all expected tools are registered."""
        registry = create_memory_registry(mock_memory_manager)

        expected_tools = [
            "create_memory",
            "search_memories",
            "delete_memory",
            "list_memories",
            "get_memory",
            "get_related_memories",
            "update_memory",
            "remember_with_image",
            "remember_screenshot",
            "memory_stats",
            "search_knowledge_graph",
        ]

        # Get available tools from registry
        tools = registry.list_tools()
        # Handle both object and dict formats
        tool_names = [t["name"] if isinstance(t, dict) else t.name for t in tools]

        for tool_name in expected_tools:
            assert tool_name in tool_names, f"Tool {tool_name} not found"

    def test_registry_with_llm_service(self, mock_memory_manager) -> None:
        """Test registry creation with LLM service (optional parameter)."""
        mock_llm = MagicMock()
        registry = create_memory_registry(mock_memory_manager, llm_service=mock_llm)

        # Should still work even though llm_service isn't used in current implementation
        assert registry is not None
        assert len(registry.list_tools()) > 0


class TestSearchMemoriesToolRegistration:
    """Tests for search_memories tool registration."""

    @pytest.mark.asyncio
    async def test_search_memories_tool_exists(self, memory_registry):
        """Test that search_memories tool is registered."""
        tools = memory_registry.list_tools()
        tool_names = [t["name"] if isinstance(t, dict) else t.name for t in tools]
        assert "search_memories" in tool_names
