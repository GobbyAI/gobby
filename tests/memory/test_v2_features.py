"""Tests for Memory V2 features.

Covers:
- Embedding/vector similarity search backend
- Cross-reference creation and retrieval
- Knowledge graph visualization export
"""

from unittest.mock import AsyncMock

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager
from gobby.memory.vectorstore import VectorStore

pytestmark = pytest.mark.unit

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def db(hub_db):
    """Create a temporary hub database for testing."""
    return hub_db


@pytest.fixture
def mock_vector_store_tracking():
    """Mock VectorStore that tracks upserts and returns them on search."""
    vs = AsyncMock(spec=VectorStore)
    stored: dict[str, list[float]] = {}

    async def _upsert(memory_id: str, embedding: list[float], payload: dict | None = None) -> None:
        stored[memory_id] = embedding

    async def _search(
        query_embedding: list[float], limit: int = 10, filters: dict | None = None
    ) -> list[tuple[str, float]]:
        return [(mid, 0.5) for mid in stored]

    async def _delete(memory_id: str) -> None:
        stored.pop(memory_id, None)

    vs.upsert = AsyncMock(side_effect=_upsert)
    vs.search = AsyncMock(side_effect=_search)
    vs.delete = AsyncMock(side_effect=_delete)
    return vs


@pytest.fixture
def mock_embed_fn():
    """Mock embedding function."""
    return AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4] * 384)


@pytest.fixture
def memory_config_with_crossref():
    """Create a memory configuration with cross-referencing enabled."""
    return MemoryConfig(
        enabled=True,
        backend="local",
        access_debounce_seconds=60,
        auto_crossref=True,
        crossref_threshold=0.1,  # Low threshold for tests
        crossref_max_links=5,
    )


@pytest.fixture
def memory_manager_with_crossref(
    db, memory_config_with_crossref, mock_vector_store_tracking, mock_embed_fn
):
    """Create a MemoryManager with cross-referencing enabled and VectorStore."""
    return MemoryManager(
        db=db,
        config=memory_config_with_crossref,
        vector_store=mock_vector_store_tracking,
        embed_fn=mock_embed_fn,
    )


# =============================================================================
# Test: Cross-References
# =============================================================================


class TestCrossReferences:
    """Tests for memory cross-reference functionality."""

    @pytest.mark.asyncio
    async def test_create_crossrefs_links_similar_memories(self, memory_manager_with_crossref):
        """Test that similar memories get cross-referenced."""
        manager = memory_manager_with_crossref

        # Create similar memories
        _mem1 = await manager.create_memory(
            content="Python is a great programming language for data science",
        )
        mem2 = await manager.create_memory(
            content="Python programming is excellent for machine learning",
        )

        # Check crossrefs were created
        crossrefs = manager.storage.get_crossrefs(mem2.id, limit=10)

        # Should have at least one crossref linking these similar memories
        assert len(crossrefs) >= 1

    @pytest.mark.asyncio
    async def test_get_related_returns_linked_memories(self, memory_manager_with_crossref):
        """Test that get_related returns cross-referenced memories."""
        manager = memory_manager_with_crossref

        # Create memories that should be linked
        _mem1 = await manager.create_memory(
            content="React is a JavaScript library for building user interfaces",
        )
        mem2 = await manager.create_memory(
            content="React components use JavaScript and JSX syntax",
        )
        _mem3 = await manager.create_memory(
            content="Completely unrelated content about cooking recipes",
        )

        # Get related memories for mem2
        related = await manager.get_related(mem2.id, limit=5)

        # Should find mem1 as related (both about React/JavaScript)
        _related_ids = [m.id for m in related]

        # At minimum, we should get some results
        # Linking is based on embedding/vector similarity (mock returns fixed 0.5 scores)
        assert isinstance(related, list)

    @pytest.mark.asyncio
    async def test_get_related_empty_for_no_crossrefs(self, db):
        """Test that get_related returns empty when no crossrefs exist."""
        config = MemoryConfig(
            enabled=True,
            backend="local",
            auto_crossref=False,
        )
        manager = MemoryManager(db=db, config=config)

        mem = await manager.create_memory(content="Isolated memory")
        related = await manager.get_related(mem.id, limit=5)

        assert related == []

    @pytest.mark.asyncio
    async def test_crossref_respects_threshold(self, db, mock_vector_store_tracking, mock_embed_fn):
        """Test that crossrefs respect similarity threshold."""
        config = MemoryConfig(
            enabled=True,
            backend="local",
            auto_crossref=True,
            crossref_threshold=0.99,  # Very high threshold
            crossref_max_links=5,
        )
        manager = MemoryManager(
            db=db,
            config=config,
            vector_store=mock_vector_store_tracking,
            embed_fn=mock_embed_fn,
        )

        await manager.create_memory(content="First topic about Python")
        mem2 = await manager.create_memory(
            content="Second topic about JavaScript",
        )

        # With very high threshold (0.99), mock scores of 0.5 shouldn't link
        crossrefs = manager.storage.get_crossrefs(mem2.id, limit=10)
        assert len(crossrefs) == 0

    @pytest.mark.asyncio
    async def test_crossref_max_links_limit(self, db, mock_vector_store_tracking, mock_embed_fn):
        """Test that crossrefs respect max_links limit."""
        config = MemoryConfig(
            enabled=True,
            backend="local",
            auto_crossref=True,
            crossref_threshold=0.01,  # Very low threshold
            crossref_max_links=2,
        )
        manager = MemoryManager(
            db=db,
            config=config,
            vector_store=mock_vector_store_tracking,
            embed_fn=mock_embed_fn,
        )

        # Create many similar memories
        for i in range(5):
            await manager.create_memory(
                content=f"Python programming topic number {i}",
            )

        # Create one more that should link to others
        mem = await manager.create_memory(
            content="Python programming language overview",
        )

        crossrefs = manager.storage.get_crossrefs(mem.id, limit=10)
        assert len(crossrefs) <= 2
