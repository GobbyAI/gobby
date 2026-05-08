"""Tests for DedupService (vector similarity dedup)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.dedup import (
    NEAR_EXACT_THRESHOLD,
    SIMILAR_THRESHOLD,
    DedupResult,
    DedupService,
)
from gobby.memory.vectorstore import VectorStoreUnavailableError

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_vector_store():
    """Mock VectorStore."""
    store = MagicMock()
    store.search = AsyncMock(return_value=[])
    store.upsert = AsyncMock()
    store.delete = AsyncMock()
    return store


@pytest.fixture
def mock_storage():
    """Mock LocalMemoryManager (SQLite storage)."""
    storage = MagicMock()
    storage.get_memory = MagicMock(return_value=None)
    return storage


@pytest.fixture
def mock_embed_fn():
    """Mock embedding function."""
    fn = AsyncMock(return_value=[0.1] * 1536)
    return fn


@pytest.fixture
def dedup_service(mock_vector_store, mock_storage, mock_embed_fn):
    """Create DedupService with all mocks."""
    return DedupService(
        vector_store=mock_vector_store,
        storage=mock_storage,
        embed_fn=mock_embed_fn,
    )


class TestDedupResult:
    """Tests for DedupResult dataclass."""

    def test_empty_result(self) -> None:
        result = DedupResult()
        assert result.added == []
        assert result.updated == []
        assert result.deleted == []

    def test_result_with_data(self) -> None:
        mock_mem = MagicMock()
        result = DedupResult(
            added=[mock_mem],
            updated=[mock_mem],
            deleted=["mem-123"],
        )
        assert len(result.added) == 1
        assert len(result.deleted) == 1


class TestProcess:
    """Tests for DedupService.process() vector similarity pipeline."""

    @pytest.mark.asyncio
    async def test_process_no_similar_returns_empty(
        self, dedup_service, mock_vector_store, mock_embed_fn
    ) -> None:
        """process() returns empty result when no similar memories found."""
        mock_vector_store.search.return_value = []

        result = await dedup_service.process(
            content="Brand new information",
            project_id="proj-1",
        )

        assert isinstance(result, DedupResult)
        assert result.added == []
        assert result.updated == []
        assert result.deleted == []
        mock_embed_fn.assert_called_once_with("Brand new information")
        mock_vector_store.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_near_exact_duplicate_noop(
        self, dedup_service, mock_vector_store, mock_embed_fn
    ) -> None:
        """process() returns empty result for near-exact duplicates (score > 0.95)."""
        mock_vector_store.search.return_value = [("mem-existing", 0.96)]

        result = await dedup_service.process(
            content="Already known fact",
            project_id="proj-1",
        )

        assert result.added == []
        assert result.updated == []
        assert result.deleted == []

    @pytest.mark.asyncio
    async def test_process_similar_updates_when_richer(
        self, dedup_service, mock_vector_store, mock_storage, mock_embed_fn
    ) -> None:
        """process() updates existing memory when new content is longer."""
        mock_vector_store.search.return_value = [("mem-old", 0.90)]

        mock_existing = MagicMock()
        mock_existing.id = "mem-old"
        mock_existing.content = "Short fact"  # 10 chars
        mock_storage.get_memory.return_value = mock_existing

        mock_updated = MagicMock()
        mock_updated.id = "mem-old"
        mock_updated.content = "Much longer and more detailed fact about something"
        mock_storage.update_memory.return_value = mock_updated

        result = await dedup_service.process(
            content="Much longer and more detailed fact about something",
            project_id="proj-1",
        )

        assert len(result.updated) == 1
        assert result.updated[0].id == "mem-old"
        mock_storage.update_memory.assert_called_once_with(
            "mem-old", content="Much longer and more detailed fact about something"
        )

    @pytest.mark.asyncio
    async def test_process_similar_noop_when_existing_sufficient(
        self, dedup_service, mock_vector_store, mock_storage, mock_embed_fn
    ) -> None:
        """process() returns empty result when existing content is longer."""
        mock_vector_store.search.return_value = [("mem-old", 0.90)]

        mock_existing = MagicMock()
        mock_existing.id = "mem-old"
        mock_existing.content = "Existing content that is much longer and more detailed"
        mock_storage.get_memory.return_value = mock_existing

        result = await dedup_service.process(
            content="Short",
            project_id="proj-1",
        )

        assert result.added == []
        assert result.updated == []
        mock_storage.update_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_below_threshold_no_action(
        self, dedup_service, mock_vector_store, mock_embed_fn
    ) -> None:
        """process() returns empty when all results are below similarity threshold."""
        mock_vector_store.search.return_value = [("mem-unrelated", 0.5)]

        result = await dedup_service.process(
            content="Something completely different",
            project_id="proj-1",
        )

        assert result.added == []
        assert result.updated == []

    @pytest.mark.asyncio
    async def test_process_fallback_on_embed_failure(
        self, dedup_service, mock_embed_fn, mock_storage, mock_vector_store
    ) -> None:
        """process() falls back to simple store when embedding fails."""
        mock_embed_fn.side_effect = [
            Exception("Embed error"),  # First call fails
            [0.1] * 1536,  # _fallback_store re-embeds
        ]

        mock_mem = MagicMock()
        mock_mem.id = "mem-fallback"
        mock_mem.content = "Raw content"
        mock_storage.create_memory = MagicMock(return_value=mock_mem)

        result = await dedup_service.process(
            content="Raw content to store",
            project_id="proj-1",
            memory_type="fact",
            tags=["fallback"],
        )

        assert len(result.added) == 1
        mock_storage.create_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_fallback_on_search_failure(
        self, dedup_service, mock_embed_fn, mock_vector_store, mock_storage
    ) -> None:
        """process() falls back to simple store when vector search fails."""
        mock_vector_store.search.side_effect = Exception("Qdrant down")

        mock_mem = MagicMock()
        mock_mem.id = "mem-fallback2"
        mock_storage.create_memory = MagicMock(return_value=mock_mem)

        result = await dedup_service.process(
            content="Some content",
            project_id="proj-1",
        )

        assert len(result.added) == 1

    @pytest.mark.asyncio
    async def test_vectorstore_unavailable_does_not_disable_embeddings(
        self, dedup_service, mock_embed_fn, mock_vector_store
    ) -> None:
        """Transient VectorStore upsert failures should not disable future embeddings."""
        mock_vector_store.upsert.side_effect = VectorStoreUnavailableError()

        await dedup_service._embed_and_upsert("mem-1", "content", "proj-1")
        await dedup_service._embed_and_upsert("mem-2", "content", "proj-1")

        assert dedup_service._embeddings_available is True
        assert mock_embed_fn.call_count == 2
        assert mock_vector_store.upsert.call_count == 2

    @pytest.mark.asyncio
    async def test_process_uses_project_filter(
        self, dedup_service, mock_vector_store, mock_embed_fn
    ) -> None:
        """process() passes project_id as filter to vector search."""
        mock_vector_store.search.return_value = []

        await dedup_service.process(content="Test", project_id="proj-42")

        call_kwargs = mock_vector_store.search.call_args[1]
        assert call_kwargs["filters"] == {"project_id": "proj-42"}

    @pytest.mark.asyncio
    async def test_process_no_project_no_filter(
        self, dedup_service, mock_vector_store, mock_embed_fn
    ) -> None:
        """process() passes None filter when no project_id."""
        mock_vector_store.search.return_value = []

        await dedup_service.process(content="Test", project_id=None)

        call_kwargs = mock_vector_store.search.call_args[1]
        assert call_kwargs["filters"] is None


class TestThresholds:
    """Tests for dedup threshold constants."""

    def test_thresholds_ordered(self) -> None:
        assert SIMILAR_THRESHOLD < NEAR_EXACT_THRESHOLD
