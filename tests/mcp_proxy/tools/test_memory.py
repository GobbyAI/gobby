"""Tests for gobby.mcp_proxy.tools.memory - additional coverage for edge cases.

Focuses on:
- remember_with_image / remember_screenshot error paths
- sync_import / sync_export
- build_turn_and_digest
- rebuild_crossrefs / rebuild_knowledge_graph
- reindex_embeddings
- search_knowledge_graph edge cases
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.memory import create_memory_registry

pytestmark = pytest.mark.unit


class MockMemory:
    """Mock memory object for tests."""

    def __init__(
        self,
        id: str = "mem-123",
        content: str = "Test memory",
        memory_type: str = "fact",
        created_at: str = "2024-01-01T00:00:00",
        updated_at: str | None = None,
        project_id: str | None = None,
        source_type: str = "agent",
        access_count: int = 0,
        tags: list[str] | None = None,
    ) -> None:
        self.id = id
        self.content = content
        self.memory_type = memory_type
        self.created_at = created_at
        self.updated_at = updated_at or created_at
        self.project_id = project_id
        self.source_type = source_type
        self.access_count = access_count
        self.tags = tags or []


@pytest.fixture
def mock_memory_manager() -> MagicMock:
    """Create a mock memory manager."""
    manager = MagicMock()
    manager.create_memory = AsyncMock(return_value=MockMemory())
    manager.search_memories = AsyncMock(return_value=[])
    manager.delete_memory = AsyncMock(return_value=True)
    manager.list_memories = MagicMock(return_value=[])
    manager.get_memory = MagicMock(return_value=MockMemory())
    manager.get_related = AsyncMock(return_value=[])
    manager.update_memory = AsyncMock(return_value=MockMemory())
    manager.get_stats = MagicMock(return_value={"total": 0})
    manager.remember_with_image = AsyncMock(return_value=MockMemory())
    manager.remember_screenshot = AsyncMock(return_value=MockMemory())
    manager.rebuild_crossrefs_for_memory = AsyncMock(return_value=2)
    manager.reindex_embeddings = AsyncMock(return_value={"success": True, "count": 5})
    manager.kg_service = None
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_llm_service() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_sync_manager() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_session_manager() -> MagicMock:
    return MagicMock()


# ─── remember_with_image ────────────────────────────────────────────────


class TestRememberWithImage:
    """Tests for remember_with_image tool."""

    @pytest.mark.asyncio
    async def test_no_llm_service(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when LLM service not configured."""
        registry = create_memory_registry(mock_memory_manager, llm_service=None)
        result = await registry.call(
            "remember_with_image",
            {"image_path": "/path/to/image.png"},
        )
        assert result["success"] is False
        assert "LLM service" in result["error"]

    @pytest.mark.asyncio
    async def test_success(
        self, mock_memory_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Successful image memory creation."""
        mock_memory_manager.remember_with_image.return_value = MockMemory(
            id="mem-img", content="A screenshot of code"
        )
        registry = create_memory_registry(mock_memory_manager, llm_service=mock_llm_service)

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "proj-1"},
        ):
            result = await registry.call(
                "remember_with_image",
                {"image_path": "/path/to/image.png", "tags": ["screenshot"]},
            )

        assert result["success"] is True
        assert result["memory"]["id"] == "mem-img"

    @pytest.mark.asyncio
    async def test_value_error(
        self, mock_memory_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Returns error on ValueError."""
        mock_memory_manager.remember_with_image.side_effect = ValueError("Invalid image")
        registry = create_memory_registry(mock_memory_manager, llm_service=mock_llm_service)

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await registry.call(
                "remember_with_image",
                {"image_path": "/bad.png"},
            )

        assert result["success"] is False
        assert "Invalid image" in result["error"]

    @pytest.mark.asyncio
    async def test_general_error(
        self, mock_memory_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Returns error on general exception."""
        mock_memory_manager.remember_with_image.side_effect = RuntimeError("IO error")
        registry = create_memory_registry(mock_memory_manager, llm_service=mock_llm_service)

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await registry.call(
                "remember_with_image",
                {"image_path": "/bad.png"},
            )

        assert result["success"] is False
        assert "IO error" in result["error"]


# ─── remember_screenshot ────────────────────────────────────────────────


class TestRememberScreenshot:
    """Tests for remember_screenshot tool."""

    @pytest.mark.asyncio
    async def test_no_llm_service(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when LLM service not configured."""
        registry = create_memory_registry(mock_memory_manager, llm_service=None)
        result = await registry.call(
            "remember_screenshot",
            {"screenshot_base64": "aGVsbG8="},
        )
        assert result["success"] is False
        assert "LLM service" in result["error"]

    @pytest.mark.asyncio
    async def test_success(
        self, mock_memory_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Successful screenshot memory creation."""
        mock_memory_manager.remember_screenshot.return_value = MockMemory(
            id="mem-ss", content="Screenshot description"
        )
        registry = create_memory_registry(mock_memory_manager, llm_service=mock_llm_service)

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "proj-1"},
        ):
            result = await registry.call(
                "remember_screenshot",
                {"screenshot_base64": "aGVsbG8=", "context": "Error screen"},
            )

        assert result["success"] is True
        assert result["memory"]["id"] == "mem-ss"

    @pytest.mark.asyncio
    async def test_value_error(
        self, mock_memory_manager: MagicMock, mock_llm_service: MagicMock
    ) -> None:
        """Returns error on ValueError."""
        mock_memory_manager.remember_screenshot.side_effect = ValueError("Bad bytes")
        registry = create_memory_registry(mock_memory_manager, llm_service=mock_llm_service)

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await registry.call(
                "remember_screenshot",
                {"screenshot_base64": "aGVsbG8="},
            )

        assert result["success"] is False
        assert "Bad bytes" in result["error"]


# ─── sync_import / sync_export ──────────────────────────────────────────


class TestSyncImport:
    """Tests for sync_import tool."""

    @pytest.mark.asyncio
    async def test_no_sync_manager(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when sync manager not available."""
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("sync_import", {})
        assert result["success"] is False
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_success(
        self,
        mock_memory_manager: MagicMock,
        mock_sync_manager: MagicMock,
    ) -> None:
        """Successful import."""
        with patch(
            "gobby.mcp_proxy.tools.memory.memory_sync_import",
            new_callable=AsyncMock,
            return_value={"imported": {"memories": 5}},
        ):
            registry = create_memory_registry(
                mock_memory_manager, memory_sync_manager=mock_sync_manager
            )
            result = await registry.call("sync_import", {})

        assert result["success"] is True
        assert result["imported"] == 5

    @pytest.mark.asyncio
    async def test_error_in_result(
        self,
        mock_memory_manager: MagicMock,
        mock_sync_manager: MagicMock,
    ) -> None:
        """Returns error when import result has error key."""
        with patch(
            "gobby.mcp_proxy.tools.memory.memory_sync_import",
            new_callable=AsyncMock,
            return_value={"error": "File not found"},
        ):
            registry = create_memory_registry(
                mock_memory_manager, memory_sync_manager=mock_sync_manager
            )
            result = await registry.call("sync_import", {})

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_exception(
        self,
        mock_memory_manager: MagicMock,
        mock_sync_manager: MagicMock,
    ) -> None:
        """Returns error on exception."""
        with patch(
            "gobby.mcp_proxy.tools.memory.memory_sync_import",
            new_callable=AsyncMock,
            side_effect=Exception("Import crashed"),
        ):
            registry = create_memory_registry(
                mock_memory_manager, memory_sync_manager=mock_sync_manager
            )
            result = await registry.call("sync_import", {})

        assert result["success"] is False
        assert "Import crashed" in result["error"]


class TestSyncExport:
    """Tests for sync_export tool."""

    @pytest.mark.asyncio
    async def test_no_sync_manager(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when sync manager not available."""
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("sync_export", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_success(
        self,
        mock_memory_manager: MagicMock,
        mock_sync_manager: MagicMock,
    ) -> None:
        """Successful export."""
        with patch(
            "gobby.mcp_proxy.tools.memory.memory_sync_export",
            new_callable=AsyncMock,
            return_value={"exported": {"memories": 10}},
        ):
            registry = create_memory_registry(
                mock_memory_manager, memory_sync_manager=mock_sync_manager
            )
            result = await registry.call("sync_export", {})

        assert result["success"] is True
        assert result["exported"] == 10

    @pytest.mark.asyncio
    async def test_error_in_result(
        self,
        mock_memory_manager: MagicMock,
        mock_sync_manager: MagicMock,
    ) -> None:
        """Returns error when export result has error key."""
        with patch(
            "gobby.mcp_proxy.tools.memory.memory_sync_export",
            new_callable=AsyncMock,
            return_value={"error": "Write failed"},
        ):
            registry = create_memory_registry(
                mock_memory_manager, memory_sync_manager=mock_sync_manager
            )
            result = await registry.call("sync_export", {})

        assert result["success"] is False


# ─── build_turn_and_digest ──────────────────────────────────────────────


class TestBuildTurnAndDigest:
    """Tests for build_turn_and_digest tool."""

    @pytest.mark.asyncio
    async def test_no_session_id(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when session_id is empty."""
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("build_turn_and_digest", {"session_id": ""})
        assert result["success"] is False
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_success(
        self,
        mock_memory_manager: MagicMock,
        mock_session_manager: MagicMock,
    ) -> None:
        """Successful turn and digest build."""
        with patch(
            "gobby.mcp_proxy.tools.memory._build_turn_and_digest",
            new_callable=AsyncMock,
            return_value={"turn_number": 1, "title": "Test"},
        ):
            registry = create_memory_registry(
                mock_memory_manager, session_manager=mock_session_manager
            )
            result = await registry.call("build_turn_and_digest", {"session_id": "sess-123"})

        assert result["success"] is True
        assert result["turn_number"] == 1

    @pytest.mark.asyncio
    async def test_returns_none_skipped(
        self,
        mock_memory_manager: MagicMock,
        mock_session_manager: MagicMock,
    ) -> None:
        """Returns skipped when result is None."""
        with patch(
            "gobby.mcp_proxy.tools.memory._build_turn_and_digest",
            new_callable=AsyncMock,
            return_value=None,
        ):
            registry = create_memory_registry(
                mock_memory_manager, session_manager=mock_session_manager
            )
            result = await registry.call("build_turn_and_digest", {"session_id": "sess-123"})

        assert result["success"] is True
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_exception(
        self,
        mock_memory_manager: MagicMock,
        mock_session_manager: MagicMock,
    ) -> None:
        """Returns error on exception."""
        with patch(
            "gobby.mcp_proxy.tools.memory._build_turn_and_digest",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM failed"),
        ):
            registry = create_memory_registry(
                mock_memory_manager, session_manager=mock_session_manager
            )
            result = await registry.call("build_turn_and_digest", {"session_id": "sess-123"})

        assert result["success"] is False
        assert "LLM failed" in result["error"]


# ─── rebuild_crossrefs ──────────────────────────────────────────────────


class TestRebuildCrossrefs:
    """Tests for rebuild_crossrefs tool."""

    @pytest.mark.asyncio
    async def test_success(self, mock_memory_manager: MagicMock) -> None:
        """Successful crossref rebuild."""
        mock_memory_manager.list_memories.return_value = [
            MockMemory(id="m1"),
            MockMemory(id="m2"),
        ]
        mock_memory_manager.rebuild_crossrefs_for_memory.return_value = 1

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_crossrefs", {})

        assert result["success"] is True
        assert result["memories_processed"] == 2
        assert result["crossrefs_created"] == 2

    @pytest.mark.asyncio
    async def test_partial_failure(self, mock_memory_manager: MagicMock) -> None:
        """Handles individual crossref failures."""
        mock_memory_manager.list_memories.return_value = [
            MockMemory(id="m1"),
            MockMemory(id="m2"),
        ]
        mock_memory_manager.rebuild_crossrefs_for_memory.side_effect = [
            Exception("fail"),
            1,
        ]

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_crossrefs", {})

        assert result["success"] is True
        assert result["crossrefs_created"] == 1

    @pytest.mark.asyncio
    async def test_list_error(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when list_memories fails."""
        mock_memory_manager.list_memories.side_effect = Exception("DB error")
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_crossrefs", {})

        assert result["success"] is False
        assert "DB error" in result["error"]


# ─── rebuild_knowledge_graph ────────────────────────────────────────────


class TestRebuildKnowledgeGraph:
    """Tests for rebuild_knowledge_graph tool."""

    @pytest.mark.asyncio
    async def test_no_kg_service(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when KG service not initialized."""
        mock_memory_manager.kg_service = None
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_knowledge_graph", {})

        assert result["success"] is False
        assert "not initialized" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_success(self, mock_memory_manager: MagicMock) -> None:
        """Successful knowledge graph rebuild."""
        mock_kg = MagicMock()
        mock_kg.add_to_graph = AsyncMock()
        mock_memory_manager.kg_service = mock_kg
        mock_memory_manager.list_memories.return_value = [
            MockMemory(id="m1"),
            MockMemory(id="m2"),
        ]

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_knowledge_graph", {})

        assert result["success"] is True
        assert result["memories_extracted"] == 2
        assert result["errors"] == 0

    @pytest.mark.asyncio
    async def test_partial_failure(self, mock_memory_manager: MagicMock) -> None:
        """Counts errors on individual extraction failures."""
        mock_kg = MagicMock()
        mock_kg.add_to_graph = AsyncMock(side_effect=[None, Exception("KG error")])
        mock_memory_manager.kg_service = mock_kg
        mock_memory_manager.list_memories.return_value = [
            MockMemory(id="m1"),
            MockMemory(id="m2"),
        ]

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_knowledge_graph", {})

        assert result["success"] is True
        assert result["memories_extracted"] == 1
        assert result["errors"] == 1


# ─── reindex_embeddings ─────────────────────────────────────────────────


class TestReindexEmbeddings:
    """Tests for reindex_embeddings tool."""

    @pytest.mark.asyncio
    async def test_success(self, mock_memory_manager: MagicMock) -> None:
        """Successful reindex."""
        mock_memory_manager.reindex_embeddings.return_value = {
            "success": True,
            "count": 10,
        }
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("reindex_embeddings", {})

        assert result["success"] is True
        assert result["count"] == 10

    @pytest.mark.asyncio
    async def test_error(self, mock_memory_manager: MagicMock) -> None:
        """Returns error on exception."""
        mock_memory_manager.reindex_embeddings.side_effect = Exception("Embedding error")
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("reindex_embeddings", {})

        assert result["success"] is False
        assert "Embedding error" in result["error"]


# ─── search_knowledge_graph ─────────────────────────────────────────────


class TestSearchKnowledgeGraph:
    """Tests for search_knowledge_graph tool."""

    @pytest.mark.asyncio
    async def test_no_kg_service(self, mock_memory_manager: MagicMock) -> None:
        """Returns empty results when KG service not available."""
        mock_memory_manager.kg_service = None
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("search_knowledge_graph", {"query": "test"})

        assert result["success"] is True
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_success(self, mock_memory_manager: MagicMock) -> None:
        """Successful KG search."""
        mock_kg = MagicMock()
        mock_kg.search_graph = AsyncMock(return_value=[{"entity": "Python"}])
        mock_memory_manager.kg_service = mock_kg

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("search_knowledge_graph", {"query": "Python", "limit": 5})

        assert result["success"] is True
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_error(self, mock_memory_manager: MagicMock) -> None:
        """Returns error on exception."""
        mock_kg = MagicMock()
        mock_kg.search_graph = AsyncMock(side_effect=Exception("KG down"))
        mock_memory_manager.kg_service = mock_kg

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("search_knowledge_graph", {"query": "test"})

        assert result["success"] is False
        assert "KG down" in result["error"]
