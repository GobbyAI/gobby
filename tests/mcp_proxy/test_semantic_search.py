"""Tests for the SemanticToolSearch module."""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.mcp_proxy.semantic_search import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    SearchResult,
    SemanticToolSearch,
    _build_tool_text,
    _compute_text_hash,
    _cosine_similarity,
)
from gobby.storage.database import LocalDatabase
from gobby.storage.mcp import LocalMCPManager

pytestmark = pytest.mark.unit


@pytest.fixture
def semantic_search(temp_db: LocalDatabase) -> SemanticToolSearch:
    """Create a SemanticToolSearch instance with temp database."""
    return SemanticToolSearch(temp_db, openai_api_key="sk-test-fake-key")


@pytest.fixture
def sample_tool(
    mcp_manager: LocalMCPManager,
    sample_project: dict,
) -> dict:
    """Create a sample tool for testing."""
    mcp_manager.upsert(
        name="test-server",
        transport="http",
        url="http://localhost:8080",
        project_id=sample_project["id"],
    )
    mcp_manager.cache_tools(
        "test-server",
        [
            {
                "name": "test_tool",
                "description": "A test tool for testing",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Max results"},
                    },
                },
            }
        ],
        project_id=sample_project["id"],
    )
    tools = mcp_manager.get_cached_tools("test-server", project_id=sample_project["id"])
    return {
        "id": tools[0].id,
        "name": tools[0].name,
        "description": tools[0].description,
        "input_schema": tools[0].input_schema,
        "server_name": "test-server",
        "project_id": sample_project["id"],
    }


class TestTextProcessing:
    """Tests for text processing functions."""

    def test_build_tool_text_basic(self) -> None:
        """Test building tool text with basic inputs."""
        text = _build_tool_text("my_tool", "Does something useful", None)
        assert "Tool: my_tool" in text
        assert "Description: Does something useful" in text

    def test_build_tool_text_with_schema(self) -> None:
        """Test building tool text with input schema."""
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "count": {"type": "integer"},
            },
        }
        text = _build_tool_text("search", "Search for items", schema)
        assert "Tool: search" in text
        assert "Description: Search for items" in text
        assert "Parameters:" in text
        assert "query (string): Search query" in text
        assert "count (integer)" in text

    def test_build_tool_text_no_description(self) -> None:
        """Test building tool text without description."""
        text = _build_tool_text("simple_tool", None, None)
        assert "Tool: simple_tool" in text
        assert "Description:" not in text


class TestSemanticToolSearchApiBase:
    """Tests for SemanticToolSearch api_base parameter."""

    def test_api_base_default_none(self, temp_db: LocalDatabase) -> None:
        """Test api_base defaults to None."""
        search = SemanticToolSearch(temp_db)
        assert search._api_base is None

    def test_api_base_custom(self, temp_db: LocalDatabase) -> None:
        """Test api_base can be set for local models."""
        search = SemanticToolSearch(
            temp_db,
            api_base="http://localhost:11434/v1",
            embedding_model="openai/nomic-embed-text",
        )
        assert search._api_base == "http://localhost:11434/v1"
        assert search.embedding_model == "openai/nomic-embed-text"

    @pytest.mark.asyncio
    async def test_embed_text_delegates_to_generate_embedding(self, temp_db: LocalDatabase) -> None:
        """Test that embed_text delegates to the shared generate_embedding router."""
        search = SemanticToolSearch(
            temp_db,
            api_base="http://localhost:11434/v1",
            openai_api_key="sk-test",
            embedding_model="text-embedding-3-small",
            embedding_dim=1536,
        )

        with patch(
            "gobby.search.embeddings.generate_embedding",
            new_callable=AsyncMock,
            return_value=[0.1, 0.2, 0.3],
        ) as mock_embed:
            result = await search.embed_text("test text")
            mock_embed.assert_called_once_with(
                text="test text",
                model="text-embedding-3-small",
                api_base="http://localhost:11434/v1",
                api_key="sk-test",
                is_query=False,
                expected_dim=1536,
            )
            assert result == [0.1, 0.2, 0.3]

    @pytest.mark.asyncio
    async def test_embed_text_local_model_no_api_key_needed(self, temp_db: LocalDatabase) -> None:
        """Test that embed_text works with local models without API key."""
        search = SemanticToolSearch(temp_db)

        with patch(
            "gobby.search.embeddings.generate_embedding",
            new_callable=AsyncMock,
            return_value=[0.1] * 768,
        ) as mock_embed:
            result = await search.embed_text("test text")
            mock_embed.assert_called_once_with(
                text="test text",
                model=DEFAULT_EMBEDDING_MODEL,
                api_base=None,
                api_key=None,
                is_query=False,
                expected_dim=DEFAULT_EMBEDDING_DIM,
            )
            assert len(result) == 768

    @pytest.mark.asyncio
    async def test_embed_text_query_mode(self, temp_db: LocalDatabase) -> None:
        """Test that embed_text passes is_query=True for search queries."""
        search = SemanticToolSearch(temp_db)

        with patch(
            "gobby.search.embeddings.generate_embedding",
            new_callable=AsyncMock,
            return_value=[0.1] * 768,
        ) as mock_embed:
            result = await search.embed_text("search query", is_query=True)
            mock_embed.assert_called_once_with(
                text="search query",
                model=DEFAULT_EMBEDDING_MODEL,
                api_base=None,
                api_key=None,
                is_query=True,
                expected_dim=DEFAULT_EMBEDDING_DIM,
            )
            assert len(result) == 768

    def test_compute_text_hash(self) -> None:
        """Test text hash computation."""
        hash1 = _compute_text_hash("hello world")
        hash2 = _compute_text_hash("hello world")
        hash3 = _compute_text_hash("different text")

        assert len(hash1) == 16
        assert hash1 == hash2
        assert hash1 != hash3


class TestSemanticToolSearch:
    """Tests for SemanticToolSearch class."""

    def test_init_default_values(self, semantic_search: SemanticToolSearch) -> None:
        """Test initialization with default values."""
        assert semantic_search.embedding_model == DEFAULT_EMBEDDING_MODEL
        assert semantic_search.embedding_dim == DEFAULT_EMBEDDING_DIM

    def test_init_custom_values(self, temp_db: LocalDatabase) -> None:
        """Test initialization with custom values."""
        search = SemanticToolSearch(
            temp_db,
            embedding_model="custom-model",
            embedding_dim=768,
        )
        assert search.embedding_model == "custom-model"
        assert search.embedding_dim == 768

    @pytest.mark.asyncio
    async def test_store_embedding_qdrant(self, temp_db: LocalDatabase) -> None:
        """Test that store_embedding upserts to Qdrant with tool metadata."""
        mock_vs = AsyncMock()
        mock_vs.get_collection_dimension = AsyncMock(return_value=DEFAULT_EMBEDDING_DIM)
        search = SemanticToolSearch(temp_db, vector_store=mock_vs)

        await search.store_embedding(
            tool_id="tool-1",
            server_name="test-server",
            project_id="proj-1",
            embedding=[0.1] * 768,
            tool_name="my_tool",
            description="Does useful things",
        )

        mock_vs.ensure_collection.assert_called_once_with("tool_embeddings", DEFAULT_EMBEDDING_DIM)
        mock_vs.upsert.assert_called_once()
        call_kwargs = mock_vs.upsert.call_args[1]
        assert call_kwargs["memory_id"] == "tool-1"
        assert call_kwargs["collection_name"] == "tool_embeddings"
        assert call_kwargs["payload"]["server_name"] == "test-server"
        assert call_kwargs["payload"]["tool_name"] == "my_tool"
        assert call_kwargs["payload"]["description"] == "Does useful things"

    @pytest.mark.asyncio
    async def test_store_embedding_no_vectorstore(
        self, semantic_search: SemanticToolSearch
    ) -> None:
        """Test store_embedding warns when no VectorStore configured."""
        # No vector_store set — should just log warning and return None
        result = await semantic_search.store_embedding(
            tool_id="tool-1",
            server_name="test-server",
            project_id="proj-1",
            embedding=[0.1] * 768,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_has_embeddings_true(self, temp_db: LocalDatabase) -> None:
        """Test has_embeddings returns True when points exist."""
        mock_vs = AsyncMock()
        mock_vs.get_collection_dimension = AsyncMock(return_value=DEFAULT_EMBEDDING_DIM)
        mock_vs.search = AsyncMock(return_value=[("tool-1", 0.5)])
        search = SemanticToolSearch(temp_db, vector_store=mock_vs)

        result = await search.has_embeddings("proj-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_has_embeddings_false(self, temp_db: LocalDatabase) -> None:
        """Test has_embeddings returns False when no points exist."""
        mock_vs = AsyncMock()
        mock_vs.get_collection_dimension = AsyncMock(return_value=DEFAULT_EMBEDDING_DIM)
        mock_vs.search = AsyncMock(return_value=[])
        search = SemanticToolSearch(temp_db, vector_store=mock_vs)

        result = await search.has_embeddings("proj-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_has_embeddings_no_vectorstore(self, semantic_search: SemanticToolSearch) -> None:
        """Test has_embeddings returns False when no VectorStore."""
        result = await semantic_search.has_embeddings("proj-1")
        assert result is False


class TestEmbeddingGeneration:
    """Tests for embedding generation methods."""

    @pytest.mark.asyncio
    async def test_embed_text(
        self,
        semantic_search: SemanticToolSearch,
    ):
        """Test generating embedding for text delegates to shared router."""
        with patch(
            "gobby.search.embeddings.generate_embedding",
            new_callable=AsyncMock,
            return_value=[0.1] * 768,
        ) as mock_embed:
            result = await semantic_search.embed_text("test text")

            assert len(result) == 768
            mock_embed.assert_called_once()

    @pytest.mark.asyncio
    async def test_embed_text_error(
        self,
        semantic_search: SemanticToolSearch,
    ):
        """Test embed_text raises RuntimeError on failure."""
        with patch(
            "gobby.search.embeddings.generate_embedding",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Embedding generation failed"),
        ):
            with pytest.raises(RuntimeError, match="Embedding generation failed"):
                await semantic_search.embed_text("test text")

    @pytest.mark.asyncio
    async def test_embed_tool(
        self,
        semantic_search: SemanticToolSearch,
        sample_tool: dict,
    ):
        """Test generating and storing embedding for a tool."""
        mock_embedding = [0.1] * DEFAULT_EMBEDDING_DIM

        with patch.object(
            semantic_search, "embed_text", new_callable=AsyncMock, return_value=mock_embedding
        ):
            result = await semantic_search.embed_tool(
                tool_id=sample_tool["id"],
                name=sample_tool["name"],
                description=sample_tool["description"],
                input_schema=sample_tool["input_schema"],
                server_name=sample_tool["server_name"],
                project_id=sample_tool["project_id"],
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_embed_tool_always_embeds(
        self,
        semantic_search: SemanticToolSearch,
        sample_tool: dict,
    ):
        """Test that embed_tool always embeds (no hash check skip)."""
        mock_embedding = [0.1] * DEFAULT_EMBEDDING_DIM

        with patch.object(
            semantic_search, "embed_text", new_callable=AsyncMock, return_value=mock_embedding
        ) as mock_embed:
            # First call
            await semantic_search.embed_tool(
                tool_id=sample_tool["id"],
                name=sample_tool["name"],
                description=sample_tool["description"],
                input_schema=sample_tool["input_schema"],
                server_name=sample_tool["server_name"],
                project_id=sample_tool["project_id"],
            )

            # Second call — should still embed (no skip)
            await semantic_search.embed_tool(
                tool_id=sample_tool["id"],
                name=sample_tool["name"],
                description=sample_tool["description"],
                input_schema=sample_tool["input_schema"],
                server_name=sample_tool["server_name"],
                project_id=sample_tool["project_id"],
            )

            assert mock_embed.call_count == 2

    @pytest.mark.asyncio
    async def test_embed_all_tools(
        self,
        semantic_search: SemanticToolSearch,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test embedding all tools for a project."""
        # Create server with multiple tools
        mcp_manager.upsert(
            name="embed-server",
            transport="http",
            url="http://localhost:8080",
            project_id=sample_project["id"],
        )
        mcp_manager.cache_tools(
            "embed-server",
            [
                {"name": "tool_a", "description": "Tool A"},
                {"name": "tool_b", "description": "Tool B"},
            ],
            project_id=sample_project["id"],
        )

        mock_embedding = [0.1] * DEFAULT_EMBEDDING_DIM

        with patch.object(
            semantic_search, "embed_text", new_callable=AsyncMock, return_value=mock_embedding
        ):
            stats = await semantic_search.embed_all_tools(
                project_id=sample_project["id"],
                mcp_manager=mcp_manager,
            )

            assert stats["embedded"] == 2
            assert stats["skipped"] == 0
            assert stats["failed"] == 0
            assert "embed-server" in stats["by_server"]
            assert stats["by_server"]["embed-server"]["embedded"] == 2

    @pytest.mark.asyncio
    async def test_embed_all_tools_with_internal_manager(
        self,
        semantic_search: SemanticToolSearch,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test embed_all_tools includes internal registry tools."""
        # Create a mock internal registry
        mock_registry = MagicMock()
        mock_registry.name = "gobby-tasks"
        mock_registry.list_tools.return_value = [
            {"name": "create_task", "brief": "Create a new task"},
            {"name": "list_tasks", "brief": "List tasks"},
        ]
        mock_registry.get_schema.side_effect = lambda name: {
            "description": f"Description for {name}",
            "inputSchema": {"type": "object", "properties": {}},
        }

        mock_internal_manager = MagicMock()
        mock_internal_manager.get_all_registries.return_value = [mock_registry]

        mock_embedding = [0.1] * DEFAULT_EMBEDDING_DIM

        with patch.object(
            semantic_search, "embed_text", new_callable=AsyncMock, return_value=mock_embedding
        ):
            stats = await semantic_search.embed_all_tools(
                project_id=sample_project["id"],
                mcp_manager=mcp_manager,
                internal_manager=mock_internal_manager,
            )

            assert stats["embedded"] == 2
            assert "gobby-tasks" in stats["by_server"]
            assert stats["by_server"]["gobby-tasks"]["embedded"] == 2

    @pytest.mark.asyncio
    async def test_embed_all_tools_handles_errors(
        self,
        semantic_search: SemanticToolSearch,
        mcp_manager: LocalMCPManager,
        sample_project: dict,
    ):
        """Test that embed_all_tools handles errors gracefully."""
        mcp_manager.upsert(
            name="error-server",
            transport="http",
            url="http://localhost:8080",
            project_id=sample_project["id"],
        )
        mcp_manager.cache_tools(
            "error-server",
            [{"name": "failing_tool", "description": "Will fail"}],
            project_id=sample_project["id"],
        )

        with patch.object(
            semantic_search,
            "embed_text",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API error"),
        ):
            stats = await semantic_search.embed_all_tools(
                project_id=sample_project["id"],
                mcp_manager=mcp_manager,
            )

            assert stats["embedded"] == 0
            assert stats["failed"] == 1
            assert len(stats["errors"]) == 1
            assert "API error" in stats["errors"][0]


class TestCosineSimilarity:
    """Tests for cosine similarity function."""

    def test_identical_vectors(self) -> None:
        """Test that identical vectors have similarity 1.0."""
        vec = [0.5, 0.5, 0.5]
        assert abs(_cosine_similarity(vec, vec) - 1.0) < 1e-6

    def test_opposite_vectors(self) -> None:
        """Test that opposite vectors have similarity -1.0."""
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [-1.0, 0.0, 0.0]
        assert abs(_cosine_similarity(vec1, vec2) - (-1.0)) < 1e-6

    def test_orthogonal_vectors(self) -> None:
        """Test that orthogonal vectors have similarity 0.0."""
        vec1 = [1.0, 0.0]
        vec2 = [0.0, 1.0]
        assert abs(_cosine_similarity(vec1, vec2)) < 1e-6

    def test_dimension_mismatch_raises(self) -> None:
        """Test that dimension mismatch raises ValueError."""
        vec1 = [1.0, 2.0]
        vec2 = [1.0, 2.0, 3.0]
        with pytest.raises(ValueError, match="dimension mismatch"):
            _cosine_similarity(vec1, vec2)

    def test_zero_vector(self) -> None:
        """Test that zero vector returns 0.0."""
        vec1 = [0.0, 0.0]
        vec2 = [1.0, 1.0]
        assert _cosine_similarity(vec1, vec2) == 0.0


class TestSearchResult:
    """Tests for SearchResult dataclass."""

    def test_to_dict(self) -> None:
        """Test converting SearchResult to dictionary."""
        result = SearchResult(
            tool_id="tool-123",
            server_name="test-server",
            tool_name="my_tool",
            description="A test tool",
            similarity=0.85678,
            embedding_id=1,
        )
        d = result.to_dict()
        assert d["tool_id"] == "tool-123"
        assert d["server_name"] == "test-server"
        assert d["tool_name"] == "my_tool"
        assert d["description"] == "A test tool"
        assert d["similarity"] == 0.8568  # Rounded to 4 decimal places
        assert "embedding_id" not in d  # Not included in dict


class TestSearchTools:
    """Tests for search_tools method (Qdrant-backed)."""

    @pytest.fixture
    def search_with_vs(self, temp_db: LocalDatabase) -> SemanticToolSearch:
        """Create SemanticToolSearch with a mock VectorStore."""
        mock_vs = AsyncMock()
        mock_vs.get_collection_dimension = AsyncMock(return_value=DEFAULT_EMBEDDING_DIM)
        mock_vs.search_with_payload = AsyncMock(return_value=[])
        return SemanticToolSearch(
            temp_db,
            openai_api_key="sk-test",
            vector_store=mock_vs,
        )

    @pytest.mark.asyncio
    async def test_search_tools_basic(
        self,
        search_with_vs: SemanticToolSearch,
        sample_project: dict,
    ):
        """Test basic tool search delegates to VectorStore and reads payload."""
        # Mock VectorStore to return ranked results with payload
        search_with_vs._vector_store.search_with_payload.return_value = [
            (
                "tool-id-1",
                0.95,
                {
                    "server_name": "search-server",
                    "tool_name": "search_tool",
                    "description": "Search for things",
                    "project_id": sample_project["id"],
                },
            ),
            (
                "tool-id-2",
                0.60,
                {
                    "server_name": "search-server",
                    "tool_name": "create_tool",
                    "description": "Create new items",
                    "project_id": sample_project["id"],
                },
            ),
        ]

        with patch.object(search_with_vs, "embed_text", new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.9] * 768

            results = await search_with_vs.search_tools(
                query="find something",
                project_id=sample_project["id"],
            )

            assert len(results) == 2
            assert results[0].tool_name == "search_tool"
            assert results[0].server_name == "search-server"
            assert results[0].description == "Search for things"
            assert results[0].similarity > results[1].similarity
            search_with_vs._vector_store.search_with_payload.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_tools_with_min_similarity(
        self,
        search_with_vs: SemanticToolSearch,
        sample_project: dict,
    ):
        """Test search filters by minimum similarity."""
        search_with_vs._vector_store.search_with_payload.return_value = [
            (
                "tool-id-1",
                0.90,
                {
                    "server_name": "minsim-server",
                    "tool_name": "relevant_tool",
                    "project_id": sample_project["id"],
                },
            ),
            (
                "tool-id-2",
                0.30,
                {
                    "server_name": "minsim-server",
                    "tool_name": "irrelevant_tool",
                    "project_id": sample_project["id"],
                },
            ),
        ]

        with patch.object(search_with_vs, "embed_text", new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.9] * 768

            results = await search_with_vs.search_tools(
                query="test",
                project_id=sample_project["id"],
                min_similarity=0.5,
            )

            assert len(results) == 1
            assert results[0].tool_name == "relevant_tool"

    @pytest.mark.asyncio
    async def test_search_tools_passes_server_filter(
        self,
        search_with_vs: SemanticToolSearch,
        sample_project: dict,
    ):
        """Test search passes server_filter to VectorStore."""
        search_with_vs._vector_store.search_with_payload.return_value = []

        with patch.object(search_with_vs, "embed_text", new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.5] * 768

            await search_with_vs.search_tools(
                query="test",
                project_id=sample_project["id"],
                server_filter="server-a",
            )

            call_kwargs = search_with_vs._vector_store.search_with_payload.call_args[1]
            assert call_kwargs["filters"]["server_name"] == "server-a"
            assert call_kwargs["collection_name"] == SemanticToolSearch.TOOL_COLLECTION

    @pytest.mark.asyncio
    async def test_search_tools_no_vectorstore(
        self,
        semantic_search: SemanticToolSearch,
        sample_project: dict,
    ):
        """Test search returns empty when no VectorStore configured."""
        with patch.object(semantic_search, "embed_text", new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.5] * 768

            results = await semantic_search.search_tools(
                query="test",
                project_id=sample_project["id"],
            )

            assert results == []

    @pytest.mark.asyncio
    async def test_search_tools_internal_tools_resolved(
        self,
        search_with_vs: SemanticToolSearch,
        sample_project: dict,
    ):
        """Test that internal tools (not in SQLite) resolve names from payload."""
        import uuid

        internal_tool_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "gobby-tasks/create_task"))

        search_with_vs._vector_store.search_with_payload.return_value = [
            (
                internal_tool_id,
                0.92,
                {
                    "server_name": "gobby-tasks",
                    "tool_name": "create_task",
                    "description": "Create a new task",
                    "project_id": sample_project["id"],
                },
            ),
        ]

        with patch.object(search_with_vs, "embed_text", new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.5] * 768

            results = await search_with_vs.search_tools(
                query="create a task",
                project_id=sample_project["id"],
            )

            assert len(results) == 1
            assert results[0].server_name == "gobby-tasks"
            assert results[0].tool_name == "create_task"
            assert results[0].description == "Create a new task"

    @pytest.mark.asyncio
    async def test_store_embedding_repairs_dimension_mismatch_and_retries(
        self,
        temp_db: LocalDatabase,
    ) -> None:
        """Test store_embedding repairs tool collection mismatch and retries once."""
        mock_vs = AsyncMock()
        mock_vs.get_collection_dimension = AsyncMock(side_effect=[DEFAULT_EMBEDDING_DIM, 1])
        mock_vs.upsert = AsyncMock(
            side_effect=[
                RuntimeError("Wrong input: Vector dimension error: expected dim: 1, got 768"),
                None,
            ]
        )
        search = SemanticToolSearch(temp_db, vector_store=mock_vs)

        await search.store_embedding(
            tool_id="tool-1",
            server_name="test-server",
            project_id="proj-1",
            embedding=[0.1] * DEFAULT_EMBEDDING_DIM,
        )

        assert mock_vs.upsert.await_count == 2
        assert mock_vs.ensure_collection.await_args_list == [
            call("tool_embeddings", DEFAULT_EMBEDDING_DIM),
            call("tool_embeddings", DEFAULT_EMBEDDING_DIM),
        ]

    @pytest.mark.asyncio
    async def test_search_tools_repairs_dimension_mismatch_and_retries(
        self,
        search_with_vs: SemanticToolSearch,
        sample_project: dict,
    ) -> None:
        """Test search_tools repairs tool collection mismatch and retries once."""
        search_with_vs._vector_store.get_collection_dimension = AsyncMock(
            side_effect=[DEFAULT_EMBEDDING_DIM, 1]
        )
        search_with_vs._vector_store.search_with_payload = AsyncMock(
            side_effect=[
                RuntimeError("Wrong input: Vector dimension error: expected dim: 1, got 768"),
                [
                    (
                        "tool-id-1",
                        0.95,
                        {
                            "server_name": "search-server",
                            "tool_name": "search_tool",
                            "description": "Search for things",
                            "project_id": sample_project["id"],
                        },
                    )
                ],
            ]
        )

        with patch.object(search_with_vs, "embed_text", new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.9] * DEFAULT_EMBEDDING_DIM

            results = await search_with_vs.search_tools(
                query="find something",
                project_id=sample_project["id"],
            )

        assert len(results) == 1
        assert results[0].tool_name == "search_tool"
        assert search_with_vs._vector_store.search_with_payload.await_count == 2
        assert search_with_vs._vector_store.ensure_collection.await_args_list == [
            call("tool_embeddings", DEFAULT_EMBEDDING_DIM),
            call("tool_embeddings", DEFAULT_EMBEDDING_DIM),
        ]

    @pytest.mark.asyncio
    async def test_runtime_repair_stays_scoped_to_tool_embeddings(
        self,
        temp_db: LocalDatabase,
    ) -> None:
        """Test runtime repair only targets the tool embeddings collection."""
        mock_vs = AsyncMock()
        mock_vs.get_collection_dimension = AsyncMock(side_effect=[DEFAULT_EMBEDDING_DIM, 1])
        mock_vs.upsert = AsyncMock(
            side_effect=[
                RuntimeError("Wrong input: Vector dimension error: expected dim: 1, got 768"),
                None,
            ]
        )
        search = SemanticToolSearch(temp_db, vector_store=mock_vs)

        await search.store_embedding(
            tool_id="tool-1",
            server_name="test-server",
            project_id="proj-1",
            embedding=[0.1] * DEFAULT_EMBEDDING_DIM,
        )

        for ensure_call in mock_vs.ensure_collection.await_args_list:
            assert ensure_call.args[0] == SemanticToolSearch.TOOL_COLLECTION

    @pytest.mark.asyncio
    async def test_get_tool_collection_dimension_logs_lookup_failures(
        self,
        temp_db: LocalDatabase,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_vs = AsyncMock()
        mock_vs.get_collection_dimension.side_effect = RuntimeError("boom")
        search = SemanticToolSearch(temp_db, vector_store=mock_vs)

        with caplog.at_level("DEBUG"):
            result = await search._get_tool_collection_dimension()

        assert result is None
        assert "Failed to read semantic tool collection dimension" in caplog.text
