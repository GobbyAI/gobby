"""Tests for DedupService (vector similarity dedup)."""

import asyncio
import logging
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client.models import FieldCondition, MatchValue

from gobby.memory.services.dedup import (
    NEAR_EXACT_THRESHOLD,
    SIMILAR_THRESHOLD,
    DedupResult,
    DedupService,
    _memory_richness_score,
)
from gobby.memory.vectorstore import VectorStoreUnavailableError
from gobby.storage.embedding_generation_state import EmbeddingGenerationLeaseLost

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_vector_store() -> MagicMock:
    """Mock VectorStore."""
    store = MagicMock()
    store.search = AsyncMock(return_value=[])
    store.upsert = AsyncMock()
    store.delete = AsyncMock()
    return store


@pytest.fixture
def mock_storage() -> MagicMock:
    """Mock LocalMemoryManager (PostgreSQL storage)."""
    storage = MagicMock()
    storage.get_memory = MagicMock(return_value=None)
    return storage


@pytest.fixture
def mock_embed_fn() -> AsyncMock:
    """Mock embedding function."""
    fn = AsyncMock(return_value=[0.1] * 1536)
    return fn


@pytest.fixture
def dedup_service(mock_vector_store: Any, mock_storage: Any, mock_embed_fn: Any) -> DedupService:
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

    def test_result_with_data(self) -> None:
        mock_mem = MagicMock()
        result = DedupResult(
            added=[mock_mem],
            updated=[mock_mem],
        )
        assert len(result.added) == 1
        assert len(result.updated) == 1


class TestProcess:
    """Tests for DedupService.process() vector similarity pipeline."""

    @pytest.mark.asyncio
    async def test_process_no_similar_returns_empty(
        self, dedup_service: DedupService, mock_vector_store: Any, mock_embed_fn: Any
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
        mock_embed_fn.assert_called_once_with("Brand new information")
        mock_vector_store.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_near_exact_duplicate_noop(
        self, dedup_service: DedupService, mock_vector_store: Any, mock_embed_fn: Any
    ) -> None:
        """process() returns empty result for near-exact duplicates (score > 0.95)."""
        mock_vector_store.search.return_value = [("mem-existing", 0.96)]

        result = await dedup_service.process(
            content="Already known fact",
            project_id="proj-1",
        )

        assert result.added == []
        assert result.updated == []

    @pytest.mark.asyncio
    async def test_process_similar_updates_when_richer(
        self,
        dedup_service: DedupService,
        mock_vector_store: Any,
        mock_storage: Any,
        mock_embed_fn: Any,
    ) -> None:
        """process() updates existing memory when new content scores richer."""
        mock_vector_store.search.return_value = [("mem-old", 0.90)]

        mock_existing = MagicMock()
        mock_existing.id = "mem-old"
        mock_existing.content = "Short fact"  # 10 chars
        storage_thread_ids: list[int] = []

        def get_memory(memory_id: str) -> Any:
            storage_thread_ids.append(threading.get_ident())
            return mock_existing

        mock_storage.get_memory.side_effect = get_memory

        mock_updated = MagicMock()
        mock_updated.id = "mem-old"
        mock_updated.content = "Much longer and more detailed fact about something"

        def update_memory(memory_id: str, *, content: str) -> Any:
            storage_thread_ids.append(threading.get_ident())
            return mock_updated

        mock_storage.update_memory.side_effect = update_memory
        event_loop_thread_id = threading.get_ident()

        result = await dedup_service.process(
            content="Much longer and more detailed fact about something",
            project_id="proj-1",
        )

        assert len(result.updated) == 1
        assert result.updated[0].id == "mem-old"
        mock_storage.update_memory.assert_called_once_with(
            "mem-old", content="Much longer and more detailed fact about something"
        )
        assert storage_thread_ids
        assert all(thread_id != event_loop_thread_id for thread_id in storage_thread_ids)

    async def test_process_similar_excluded_source_is_concurrent_noop(
        self,
        dedup_service: DedupService,
        mock_vector_store: Any,
        mock_storage: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Background dedup does not copy already-stored source content to another memory."""
        mock_vector_store.search.return_value = [
            ("mem-source", 0.99),
            ("mem-old", 0.90),
        ]
        mock_existing = MagicMock(content="Short fact")
        mock_storage.get_memory.return_value = mock_existing

        with caplog.at_level(logging.DEBUG, logger="gobby.memory.services.dedup"):
            results = await asyncio.gather(
                *(
                    dedup_service.process(
                        content="Much longer and more detailed fact about something",
                        project_id="proj-1",
                        exclude_memory_id="mem-source",
                    )
                    for _ in range(2)
                )
            )

        assert all(result.added == [] and result.updated == [] for result in results)
        assert mock_storage.get_memory.call_count == 2
        mock_storage.update_memory.assert_not_called()
        assert caplog.text.count("already stored by excluded source memory mem-source") == 2

    @pytest.mark.asyncio
    async def test_process_skips_excluded_self_match_without_copying_to_duplicate(
        self,
        dedup_service: DedupService,
        mock_vector_store: Any,
        mock_storage: Any,
    ) -> None:
        """A just-created memory already owns richer content found after its self-match."""
        content = "Much longer and more detailed fact about something"
        mock_vector_store.search.return_value = [("mem-new", 1.0), ("mem-old", 0.90)]

        mock_existing = MagicMock()
        mock_existing.content = "Short fact"
        mock_storage.get_memory.return_value = mock_existing

        result = await dedup_service.process(
            content=content,
            project_id="proj-1",
            exclude_memory_id="mem-new",
        )

        assert result.updated == []
        mock_storage.get_memory.assert_called_once_with("mem-old")
        mock_storage.update_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_similar_noop_when_existing_sufficient(
        self,
        dedup_service: DedupService,
        mock_vector_store: Any,
        mock_storage: Any,
        mock_embed_fn: Any,
    ) -> None:
        """process() returns empty result when existing content scores richer."""
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
    async def test_process_similar_updates_with_shorter_but_more_detailed_content(
        self, dedup_service: DedupService, mock_vector_store: Any, mock_storage: Any
    ) -> None:
        """Structured details can replace vague longer text."""
        mock_vector_store.search.return_value = [("mem-old", 0.90)]

        mock_existing = MagicMock()
        mock_existing.id = "mem-old"
        mock_existing.content = (
            "Gobby keeps a local database for user information and project state."
        )
        mock_storage.get_memory.return_value = mock_existing

        richer = "Memory DB: ~/.gobby/hub-postgres.db; scope=project; task #14567."
        mock_updated = MagicMock()
        mock_updated.id = "mem-old"
        mock_updated.content = richer
        mock_storage.update_memory.return_value = mock_updated

        result = await dedup_service.process(content=richer, project_id="proj-1")

        assert len(result.updated) == 1
        mock_storage.update_memory.assert_called_once_with("mem-old", content=richer)

    @pytest.mark.asyncio
    async def test_process_similar_keeps_concise_detail_over_longer_vague_content(
        self, dedup_service: DedupService, mock_vector_store: Any, mock_storage: Any
    ) -> None:
        """Longer vague prose does not replace concise operational detail."""
        mock_vector_store.search.return_value = [("mem-old", 0.90)]

        mock_existing = MagicMock()
        mock_existing.id = "mem-old"
        mock_existing.content = "API root: https://api.example.test; timeout=30."
        mock_storage.get_memory.return_value = mock_existing

        result = await dedup_service.process(
            content=(
                "The API setup has several useful operational details that are worth "
                "remembering for future work on integrations and request handling."
            ),
            project_id="proj-1",
        )

        assert result.updated == []
        mock_storage.update_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_below_threshold_no_action(
        self, dedup_service: DedupService, mock_vector_store: Any, mock_embed_fn: Any
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
        self,
        dedup_service: DedupService,
        mock_embed_fn: Any,
        mock_storage: Any,
        mock_vector_store: Any,
    ) -> None:
        """process() falls back to simple store when embedding fails."""
        mock_embed_fn.side_effect = [
            Exception("Embed error"),  # First call fails
            [0.1] * 1536,  # _fallback_store re-embeds
        ]

        mock_mem = MagicMock()
        mock_mem.id = "mem-fallback"
        mock_mem.content = "Raw content"
        storage_thread_ids: list[int] = []

        def create_memory(**kwargs: Any) -> Any:
            storage_thread_ids.append(threading.get_ident())
            return mock_mem

        mock_storage.create_memory = MagicMock(side_effect=create_memory)
        event_loop_thread_id = threading.get_ident()

        result = await dedup_service.process(
            content="Raw content to store",
            project_id="proj-1",
            memory_type="fact",
            tags=["fallback"],
        )

        assert len(result.added) == 1
        mock_storage.create_memory.assert_called_once()
        assert storage_thread_ids
        assert storage_thread_ids[0] != event_loop_thread_id
        assert mock_embed_fn.await_count == 2
        mock_vector_store.upsert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_process_retries_embedding_until_provider_recovers(
        self,
        dedup_service: DedupService,
        mock_embed_fn: Any,
        mock_storage: Any,
        mock_vector_store: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Failed embeds are retried on later dedup calls while warnings stay rate-limited."""
        mock_embed_fn.side_effect = [
            RuntimeError("provider down"),
            RuntimeError("provider still down"),
            [0.1] * 1536,
        ]
        mock_memory = MagicMock()
        mock_memory.id = "mem-fallback"
        mock_storage.create_memory.return_value = mock_memory
        mock_vector_store.search.return_value = []

        with caplog.at_level(logging.DEBUG, logger="gobby.memory.services.dedup"):
            first = await dedup_service.process(content="first", project_id="proj-1")
            second = await dedup_service.process(content="second", project_id="proj-1")

        assert first.added == [mock_memory]
        assert second.added == []
        assert mock_embed_fn.await_count == 3
        mock_vector_store.search.assert_awaited_once()
        embedding_records = [
            record
            for record in caplog.records
            if record.name == "gobby.memory.services.dedup"
            and record.message.startswith("Embedding failed")
        ]
        assert [record.levelno for record in embedding_records] == [logging.WARNING, logging.DEBUG]
        assert not hasattr(dedup_service, "_embeddings_available")

    @pytest.mark.asyncio
    async def test_process_fallback_on_search_failure(
        self,
        dedup_service: DedupService,
        mock_embed_fn: Any,
        mock_vector_store: Any,
        mock_storage: Any,
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

    async def test_fenced_lease_search_uses_rate_limited_fallback(
        self,
        dedup_service: DedupService,
        mock_vector_store: Any,
        mock_storage: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        error = EmbeddingGenerationLeaseLost("Embedding generation serving is fenced")
        mock_vector_store.search.side_effect = error
        mock_memory = MagicMock(id="mem-fenced")
        mock_storage.create_memory = MagicMock(return_value=mock_memory)

        with caplog.at_level(logging.WARNING, logger="gobby.memory.services.dedup"):
            result = await dedup_service.process(
                content="Some content",
                project_id="proj-1",
            )

        assert result.added == [mock_memory]
        assert "Vector search unavailable, falling back to simple store" in caplog.text
        assert "Vector search failed" not in caplog.text

    @pytest.mark.asyncio
    async def test_vectorstore_unavailable_does_not_disable_embeddings(
        self, dedup_service: DedupService, mock_embed_fn: Any, mock_vector_store: Any
    ) -> None:
        """Transient VectorStore upsert failures should not disable future embeddings."""
        mock_vector_store.upsert.side_effect = VectorStoreUnavailableError()

        await dedup_service._embed_and_upsert("mem-1", "content", "proj-1", False, "fact")
        await dedup_service._embed_and_upsert("mem-2", "content", "proj-1", False, "fact")

        assert mock_embed_fn.call_count == 2
        assert mock_vector_store.upsert.call_count == 2
        assert not hasattr(dedup_service, "_embeddings_available")

    @pytest.mark.asyncio
    async def test_process_uses_project_plus_global_filter(
        self, dedup_service: DedupService, mock_vector_store: Any, mock_embed_fn: Any
    ) -> None:
        mock_vector_store.search.return_value = []

        await dedup_service.process(content="Test", project_id="proj-42")

        filters = mock_vector_store.search.call_args.kwargs["filters"]
        assert filters.must is None
        assert filters.should is not None
        assert [
            condition.match.value
            for condition in filters.should
            if isinstance(condition, FieldCondition) and isinstance(condition.match, MatchValue)
        ] == ["proj-42", True]

    @pytest.mark.asyncio
    async def test_process_global_scope_excludes_project_memories(
        self, dedup_service: DedupService, mock_vector_store: Any, mock_embed_fn: Any
    ) -> None:
        mock_vector_store.search.return_value = []

        await dedup_service.process(content="Test", project_id="global-owner", is_global=True)

        filters = mock_vector_store.search.call_args.kwargs["filters"]
        assert filters.should is None
        assert filters.must is not None
        assert [
            condition.match.value
            for condition in filters.must
            if isinstance(condition, FieldCondition) and isinstance(condition.match, MatchValue)
        ] == [True]


class TestThresholds:
    """Tests for dedup threshold constants."""

    def test_thresholds_ordered(self) -> None:
        assert SIMILAR_THRESHOLD < NEAR_EXACT_THRESHOLD

    def test_richness_score_prioritizes_details_before_length(self) -> None:
        detailed = "API root: https://api.example.test; timeout=30."
        vague = "This API setup has useful operational details for future integration work."

        assert _memory_richness_score(detailed) > _memory_richness_score(vague)

    def test_richness_score_does_not_count_version_dots_as_sentences(self) -> None:
        versioned = _memory_richness_score("Use SDK v1.2.3 for retries.")
        plain = _memory_richness_score("Use SDK v123 for retries.")

        assert versioned[2] == plain[2] == 1
