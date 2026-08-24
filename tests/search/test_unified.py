"""Tests for the unified search module."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.ai.embeddings import EmbeddingGenerationError
from gobby.search import (
    EmbeddingBackend,
    FallbackEvent,
    SearchConfig,
    SearchMode,
    UnifiedSearcher,
)
from gobby.search.backends import embedding as embedding_module
from gobby.search.keyword import BM25SearchBackend, KeywordAsyncSearchBackend, SearchHit
from gobby.search.similarity import cosine_similarity

pytestmark = pytest.mark.unit


@pytest.fixture
def db() -> SimpleNamespace:
    """Lightweight database seam for UnifiedSearcher keyword backend tests."""
    return SimpleNamespace(dialect="postgres")


def _make_searcher(
    db,
    config: SearchConfig | None = None,
    event_callback=None,
) -> UnifiedSearcher:
    """Helper to create UnifiedSearcher with required keyword params."""
    return UnifiedSearcher(
        config,
        event_callback=event_callback,
        db=db,
        fts_table="skills_fts",
        fts_content_table="skills",
        fts_weights=(10.0, 5.0, 2.0, 2.0),
    )


def _make_openai_client(dim: int) -> AsyncMock:
    """Create a mock AsyncOpenAI client with deterministic vector size."""
    mock_client = AsyncMock()

    @dataclass
    class FakeItem:
        embedding: list[float]
        index: int

    @dataclass
    class FakeResponse:
        data: list[FakeItem]

    async def fake_create(model: str, input: list[str]) -> SimpleNamespace:
        response = FakeResponse([FakeItem([0.1] * dim, index) for index, _ in enumerate(input)])
        return SimpleNamespace(parse=lambda: response)

    create_mock: AsyncMock = AsyncMock(side_effect=fake_create)
    mock_client.embeddings.with_raw_response.create = create_mock
    return mock_client


class TestSearchConfig:
    """Tests for SearchConfig."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = SearchConfig()

        assert config.mode == "auto"
        assert config.keyword_weight == 0.4
        assert config.embedding_weight == 0.6
        assert config.notify_on_fallback is True

    def test_custom_config(self) -> None:
        """Test custom configuration values."""
        config = SearchConfig(
            mode="hybrid",
            keyword_weight=0.5,
            embedding_weight=0.5,
        )

        assert config.mode == "hybrid"
        assert config.keyword_weight == 0.5

    def test_get_mode_enum(self) -> None:
        """Test get_mode_enum returns correct SearchMode."""
        config = SearchConfig(mode="auto")
        assert config.get_mode_enum() == SearchMode.AUTO

        config = SearchConfig(mode="keyword")
        assert config.get_mode_enum() == SearchMode.KEYWORD

        config = SearchConfig(mode="embedding")
        assert config.get_mode_enum() == SearchMode.EMBEDDING

        config = SearchConfig(mode="hybrid")
        assert config.get_mode_enum() == SearchMode.HYBRID

    def test_get_normalized_weights(self) -> None:
        """Test weight normalization."""
        config = SearchConfig(keyword_weight=0.4, embedding_weight=0.6)
        keyword, embedding = config.get_normalized_weights()
        assert keyword == 0.4
        assert embedding == 0.6

        # Test non-standard weights
        config = SearchConfig(keyword_weight=1.0, embedding_weight=1.0)
        keyword, embedding = config.get_normalized_weights()
        assert keyword == 0.5
        assert embedding == 0.5

        # Test zero weights fallback
        config = SearchConfig(keyword_weight=0.0, embedding_weight=0.0)
        keyword, embedding = config.get_normalized_weights()
        assert keyword == 0.5
        assert embedding == 0.5


class TestSearchMode:
    """Tests for SearchMode enum."""

    def test_enum_values(self) -> None:
        """Test SearchMode enum values."""
        assert SearchMode.KEYWORD.value == "keyword"
        assert SearchMode.EMBEDDING.value == "embedding"
        assert SearchMode.AUTO.value == "auto"
        assert SearchMode.HYBRID.value == "hybrid"

    def test_string_equality(self) -> None:
        """Test SearchMode string comparison."""
        assert SearchMode.KEYWORD == "keyword"
        assert SearchMode.AUTO == "auto"


class TestFallbackEvent:
    """Tests for FallbackEvent dataclass."""

    def test_basic_event(self) -> None:
        """Test basic fallback event creation."""
        event = FallbackEvent(reason="API key not configured")

        assert event.reason == "API key not configured"
        assert event.original_error is None
        assert event.mode == "auto"
        assert event.items_reindexed == 0

    def test_event_with_error(self) -> None:
        """Test fallback event with error."""
        error = RuntimeError("Connection failed")
        event = FallbackEvent(
            reason="Embedding failed",
            original_error=error,
            mode="auto",
            items_reindexed=10,
        )

        assert event.reason == "Embedding failed"
        assert event.original_error is error
        assert event.items_reindexed == 10

    def test_to_dict(self) -> None:
        """Test to_dict serialization."""
        event = FallbackEvent(reason="Test reason", mode="hybrid")
        data = event.to_dict()

        assert data["reason"] == "Test reason"
        assert data["mode"] == "hybrid"
        assert "timestamp" in data

    def test_str_representation(self) -> None:
        """Test string representation."""
        event = FallbackEvent(reason="Test")
        assert "FallbackEvent: Test" in str(event)

        error = ValueError("bad value")
        event = FallbackEvent(reason="With error", original_error=error)
        assert "FallbackEvent: With error" in str(event)
        assert "bad value" in str(event)


class TestUnifiedSearcher:
    """Tests for UnifiedSearcher."""

    @pytest.mark.asyncio
    async def test_keyword_mode(self, db) -> None:
        """Test keyword-only mode."""
        config = SearchConfig(mode="keyword")
        searcher = _make_searcher(db, config)

        items = [
            ("id1", "hello world"),
            ("id2", "foo bar baz"),
        ]

        await searcher.fit_async(items)
        assert searcher.get_active_backend() == "keyword"
        assert not searcher.is_using_fallback()

    @pytest.mark.asyncio
    async def test_auto_mode_no_api_key(self, db) -> None:
        """Test auto mode falls back to keyword when no API key."""
        config = SearchConfig(
            mode="auto",
            embedding_model="text-embedding-3-small",
            embedding_api_key=None,
        )

        with patch(
            "gobby.search.unified.EmbeddingService.is_reachable",
            new_callable=AsyncMock,
            return_value=False,
        ):
            fallback_events: list[FallbackEvent] = []
            searcher = _make_searcher(
                db, config, event_callback=lambda e: fallback_events.append(e)
            )

            items = [("id1", "test content")]
            await searcher.fit_async(items)

            assert searcher.get_active_backend() == "keyword"
            assert searcher.is_using_fallback()
            assert len(fallback_events) == 1
            assert "unavailable" in fallback_events[0].reason.lower()

    def test_embedding_backend_preserves_daemon_embedding_config(self, db) -> None:
        """UnifiedSearcher forwards daemon embedding endpoint and dim unchanged."""
        searcher = UnifiedSearcher(
            SearchConfig(mode="auto"),
            db=db,
            fts_table="skills_fts",
            embedding_model="text-embedding-nomic-embed-text-v1.5@f16",
            embedding_api_base="http://localhost:1234/v1",
            embedding_api_key="local-key",
            embedding_dim=768,
        )

        backend = searcher._get_embedding_backend()

        assert backend._model == "text-embedding-nomic-embed-text-v1.5@f16"
        assert backend._api_base == "http://localhost:1234/v1"
        assert backend._api_key == "local-key"
        assert backend._dim == 768

    @pytest.mark.asyncio
    async def test_auto_mode_embedding_available(self, db) -> None:
        """Test auto mode uses embedding when available."""
        config = SearchConfig(mode="auto")

        mock_embeddings = [[0.1, 0.2, 0.3]] * 2

        with (
            patch(
                "gobby.search.unified.EmbeddingService.is_reachable",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embeddings",
                new_callable=AsyncMock,
                return_value=mock_embeddings,
            ),
        ):
            searcher = _make_searcher(db, config)
            items = [("id1", "hello"), ("id2", "world")]

            await searcher.fit_async(items)

            assert searcher.get_active_backend() == "embedding"
            assert not searcher.is_using_fallback()

    @pytest.mark.asyncio
    async def test_auto_mode_embedding_fails_at_runtime(self, db) -> None:
        """Test auto mode falls back when embedding fails at runtime."""
        config = SearchConfig(mode="auto")

        with (
            patch(
                "gobby.search.unified.EmbeddingService.is_reachable",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embeddings",
                new_callable=AsyncMock,
                side_effect=RuntimeError("API error"),
            ),
        ):
            fallback_events: list[FallbackEvent] = []
            searcher = _make_searcher(
                db, config, event_callback=lambda e: fallback_events.append(e)
            )

            items = [("id1", "test")]
            await searcher.fit_async(items)

            assert searcher.is_using_fallback()
            assert len(fallback_events) == 1

    @pytest.mark.asyncio
    async def test_embedding_mode_fails_without_key(self, db) -> None:
        """Test embedding mode raises 'not configured' when no key/base."""
        config = SearchConfig(mode="embedding")

        with patch("gobby.search.unified.EmbeddingService.is_configured", return_value=False):
            searcher = _make_searcher(db, config)

            with pytest.raises(RuntimeError, match="not configured"):
                await searcher.fit_async([("id1", "test")])

    @pytest.mark.asyncio
    async def test_embedding_mode_fails_when_unreachable(self, db) -> None:
        """Test embedding mode raises 'unreachable' when configured but down."""
        config = SearchConfig(
            mode="embedding",
            embedding_model="nomic-embed-text",
            embedding_api_base="http://127.0.0.1:1",
        )

        with (
            patch("gobby.search.unified.EmbeddingService.is_configured", return_value=True),
            patch(
                "gobby.search.unified.EmbeddingService.is_reachable",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            searcher = _make_searcher(db, config)

            with pytest.raises(RuntimeError, match="unreachable"):
                await searcher.fit_async([("id1", "test")])

    @pytest.mark.asyncio
    async def test_hybrid_mode(self, db) -> None:
        """Test hybrid mode combines both backends."""
        config = SearchConfig(
            mode="hybrid",
            keyword_weight=0.5,
            embedding_weight=0.5,
        )

        mock_embeddings = [[0.1, 0.2, 0.3]] * 2
        mock_query_embedding = [0.1, 0.2, 0.3]

        with (
            patch("gobby.search.unified.EmbeddingService.is_configured", return_value=True),
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embeddings",
                new_callable=AsyncMock,
                return_value=mock_embeddings,
            ),
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embedding",
                new_callable=AsyncMock,
                return_value=mock_query_embedding,
            ),
        ):
            searcher = _make_searcher(db, config)
            items = [("id1", "hello world"), ("id2", "goodbye world")]

            await searcher.fit_async(items)
            results = await searcher.search_async("hello")

            assert searcher.get_active_backend() == "hybrid"
            assert len(results) > 0

    @pytest.mark.asyncio
    async def test_get_stats(self, db) -> None:
        """Test get_stats returns comprehensive info."""
        config = SearchConfig(mode="keyword")
        searcher = _make_searcher(db, config)

        items = [("id1", "test content")]
        await searcher.fit_async(items)

        stats = searcher.get_stats()

        assert stats["mode"] == "keyword"
        assert stats["fitted"] is True
        assert stats["active_backend"] == "keyword"
        assert stats["using_fallback"] is False
        assert stats["item_count"] == 1

    @pytest.mark.asyncio
    async def test_clear(self, db) -> None:
        """Test clear resets all state."""
        config = SearchConfig(mode="keyword")
        searcher = _make_searcher(db, config)

        await searcher.fit_async([("id1", "test")])
        searcher.clear()

        assert not searcher._fitted
        assert searcher.get_active_backend() == "none"
        assert not searcher.is_using_fallback()

    @pytest.mark.asyncio
    async def test_needs_refit(self, db) -> None:
        """Test needs_refit tracking."""
        config = SearchConfig(mode="keyword")
        searcher = _make_searcher(db, config)

        assert searcher.needs_refit()

        await searcher.fit_async([("id1", "test")])
        assert not searcher.needs_refit()

    def test_mark_update_marks_keyword_and_embedding_backends_stale(self, db) -> None:
        config = SearchConfig(mode="hybrid")
        searcher = _make_searcher(db, config)
        keyword_backend = KeywordAsyncSearchBackend(db, "skills")
        embedding_backend = EmbeddingBackend()
        embedding_backend._fitted = True
        searcher._keyword_backend = keyword_backend
        searcher._embedding_backend = embedding_backend
        searcher._fitted = True

        searcher.mark_update()

        assert keyword_backend.needs_refit()
        assert embedding_backend.needs_refit()

    @pytest.mark.asyncio
    async def test_search_unfitted_returns_empty(self, db) -> None:
        """Test search before fitting returns empty."""
        config = SearchConfig(mode="keyword")
        searcher = _make_searcher(db, config)

        results = await searcher.search_async("test")
        assert results == []

    @pytest.mark.asyncio
    async def test_fallback_event_callback(self, db) -> None:
        """Test fallback event callback is called."""
        config = SearchConfig(mode="auto", notify_on_fallback=True)

        events: list[FallbackEvent] = []

        def callback(event: FallbackEvent) -> None:
            events.append(event)

        with patch(
            "gobby.search.unified.EmbeddingService.is_reachable",
            new_callable=AsyncMock,
            return_value=False,
        ):
            searcher = _make_searcher(db, config, event_callback=callback)
            await searcher.fit_async([("id1", "test")])

        assert len(events) == 1
        assert events[0].mode == "auto"

    @pytest.mark.asyncio
    async def test_hybrid_partial_failure(self, db) -> None:
        """Test hybrid mode continues with keyword search when embedding fails."""
        config = SearchConfig(mode="hybrid")

        with (
            patch("gobby.search.unified.EmbeddingService.is_configured", return_value=True),
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embeddings",
                new_callable=AsyncMock,
                side_effect=RuntimeError("API error"),
            ),
        ):
            searcher = _make_searcher(db, config)
            await searcher.fit_async([("id1", "test")])

            assert searcher.get_active_backend() == "keyword"


class TestDeprecatedKeywordSearch:
    """Tests for the legacy PostgreSQL keyword fallback used by fixtures."""

    @pytest.mark.asyncio
    async def test_fit_items_allow_partial_token_matches_with_ranked_scores(self) -> None:
        """Partial token matches are returned with lower scores than full matches."""
        backend = KeywordAsyncSearchBackend(SimpleNamespace(dialect="postgres"), "skills")
        await backend.fit_async(
            [
                ("full", "deprecated postgres import"),
                ("partial", "deprecated postgres"),
                ("single", "deprecated"),
                ("none", "unrelated"),
            ]
        )

        results = await backend.search_async("deprecated postgres import", top_k=10)

        assert results == [
            ("full", 1.0),
            ("partial", 2 / 3),
            ("single", 1 / 3),
        ]


class TestKeywordAsyncSearchBackend:
    @pytest.mark.asyncio
    async def test_search_async_runs_database_search_off_event_loop(self) -> None:
        backend = KeywordAsyncSearchBackend(SimpleNamespace(dialect="postgres"), "skills")
        await backend.fit_async([("inside", "shared query")])
        loop_thread = threading.get_ident()
        worker_threads: list[int] = []
        search_started = threading.Event()
        release_search = threading.Event()

        def blocking_search(*_args: object, **_kwargs: object) -> list[SearchHit]:
            worker_threads.append(threading.get_ident())
            search_started.set()
            if not release_search.wait(timeout=2):
                raise TimeoutError("test did not release blocking search")
            return [SearchHit(id="inside", score=1.0)]

        backend._backend.search = blocking_search

        search_task = asyncio.create_task(backend.search_async("shared query", top_k=10))
        try:
            started = await asyncio.to_thread(search_started.wait, 2)
            assert started, "blocking search did not start"
            assert not search_task.done(), "blocking search stalled the event loop"
        finally:
            release_search.set()

        assert await search_task == [("inside", 1.0)]
        assert len(worker_threads) == 1
        assert worker_threads[0] != loop_thread

    @pytest.mark.asyncio
    async def test_search_async_uses_consistent_fit_snapshot_during_clear(self) -> None:
        backend = KeywordAsyncSearchBackend(SimpleNamespace(dialect="postgres"), "skills")
        await backend.fit_async([("inside", "shared query")])
        search_started = threading.Event()
        release_search = threading.Event()

        def blocking_search(*_args: object, **_kwargs: object) -> list[SearchHit]:
            search_started.set()
            assert release_search.wait(timeout=1)
            raise RuntimeError("backend unavailable")

        backend._backend.search = blocking_search

        search_task = asyncio.create_task(backend.search_async("shared query", top_k=10))
        assert await asyncio.to_thread(search_started.wait, 1)
        backend.clear()
        release_search.set()

        assert await search_task == [("inside", 1.0)]

    @pytest.mark.asyncio
    async def test_get_stats_uses_fitted_count_without_database_query(self) -> None:
        fetchone = MagicMock(side_effect=AssertionError("get_stats queried the database"))
        backend = KeywordAsyncSearchBackend(
            SimpleNamespace(dialect="postgres", fetchone=fetchone),
            "skills",
        )
        await backend.fit_async([("one", "first"), ("two", "second")])

        stats = backend.get_stats()

        assert stats["backend_type"] == "pg_search_bm25"
        assert stats["document_count"] == 2
        assert stats["fitted"] is True
        fetchone.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_restricts_database_hits_to_fitted_ids(self) -> None:
        backend = KeywordAsyncSearchBackend(SimpleNamespace(dialect="postgres"), "skills")
        native_search = MagicMock(
            return_value=[
                SearchHit(id="outside", score=1.0),
                SearchHit(id="inside", score=0.5),
            ]
        )
        backend._backend.search = native_search
        await backend.fit_async([("inside", "shared query")])

        results = await backend.search_async("shared query", top_k=10)

        assert results == [("inside", 0.5)]
        native_search.assert_called_once_with(
            "shared query",
            10,
            allowed_ids=("inside",),
        )

    async def test_successful_empty_database_search_does_not_use_fitted_fallback(self) -> None:
        backend = KeywordAsyncSearchBackend(SimpleNamespace(dialect="postgres"), "skills")
        backend._backend.search = MagicMock(return_value=[])
        await backend.fit_async([("inside", "shared query")])

        with patch(
            "gobby.search.keyword._search_fitted_items",
            side_effect=AssertionError("fallback must only run after backend errors"),
        ):
            results = await backend.search_async("shared query", top_k=10)

        assert results == []

    @pytest.mark.parametrize(
        ("query", "content"),
        [
            ("你好", "你好 世界"),
            ("привет", "ПРИВЕТ мир"),
            ("café", "CAFÉ menu"),
        ],
    )
    async def test_backend_error_fallback_tokenizes_unicode(
        self,
        query: str,
        content: str,
    ) -> None:
        backend = KeywordAsyncSearchBackend(SimpleNamespace(dialect="postgres"), "skills")
        backend._backend.search = MagicMock(side_effect=RuntimeError("backend unavailable"))
        await backend.fit_async([("match", content), ("other", "unrelated")])

        results = await backend.search_async(query, top_k=10)

        assert results == [("match", 1.0)]

    @pytest.mark.asyncio
    async def test_empty_fit_returns_no_results_without_querying_database(self) -> None:
        backend = KeywordAsyncSearchBackend(SimpleNamespace(dialect="postgres"), "skills")
        native_search = MagicMock(return_value=[SearchHit(id="outside", score=1.0)])
        backend._backend.search = native_search
        await backend.fit_async([])

        assert await backend.search_async("shared query", top_k=10) == []
        native_search.assert_not_called()

    def test_skills_table_supports_project_and_enabled_filters(self) -> None:
        class RecordingHub:
            def __init__(self) -> None:
                self.sql = ""
                self.params: tuple[object, ...] = ()

            def fetchall(self, sql: str, params: tuple[object, ...]) -> list[object]:
                self.sql = sql
                self.params = params
                return []

        hub = RecordingHub()
        backend = BM25SearchBackend(hub, "skills")

        assert (
            backend.search(
                "shared query",
                10,
                filters={"project_id": "project-a", "enabled": True},
            )
            == []
        )
        assert "skills.project_id = %s" in hub.sql
        assert "skills.enabled = %s" in hub.sql
        assert hub.params == ("shared query", "shared query", "shared query", "project-a", True, 10)

    def test_unknown_table_filter_raises(self) -> None:
        backend = BM25SearchBackend(SimpleNamespace(dialect="postgres"), "skills")

        with pytest.raises(ValueError, match="unsupported filter 'tenant_id'.*'skills'"):
            backend.search("shared query", 10, filters={"tenant_id": "other"})

    @pytest.mark.parametrize(
        "updated_items",
        [
            [("id1", "alpha"), ("id2", "beta")],
            [("id1", "alpha updated")],
            [],
        ],
    )
    @pytest.mark.asyncio
    async def test_mark_update_needs_refit_until_next_fit(
        self,
        updated_items: list[tuple[str, str]],
    ) -> None:
        backend = KeywordAsyncSearchBackend(SimpleNamespace(dialect="postgres"), "skills")
        await backend.fit_async([("id1", "alpha")])
        assert not backend.needs_refit()

        backend.mark_update()

        assert backend.needs_refit()
        await backend.fit_async(updated_items)
        assert not backend.needs_refit()


class TestEmbeddingBackend:
    """Tests for EmbeddingBackend."""

    def test_from_config(self) -> None:
        """Test creating backend from EmbeddingsConfig."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig(
            model="openai/nomic-embed-text",
            api_base="http://localhost:11434/v1",
        )

        backend = EmbeddingBackend.from_config(config)

        assert backend._model == "openai/nomic-embed-text"
        assert backend._api_base == "http://localhost:11434/v1"
        assert backend._dim == 768

    @pytest.mark.asyncio
    async def test_fit_and_search(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test fit and search with mocked embeddings."""
        backend = EmbeddingBackend()

        mock_fit_embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        mock_query_embedding = [0.1, 0.2, 0.3]  # Similar to first item

        with (
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embeddings",
                new_callable=AsyncMock,
                return_value=mock_fit_embeddings,
            ),
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embedding",
                new_callable=AsyncMock,
                return_value=mock_query_embedding,
            ),
        ):
            items = [("id1", "hello"), ("id2", "world")]
            logger_name = "gobby.search.backends.embedding"
            with caplog.at_level(logging.DEBUG, logger=logger_name):
                await backend.fit_async(items)

            records = [
                record
                for record in caplog.records
                if record.getMessage() == "Embedding index built"
            ]
            assert len(records) == 1
            assert records[0].__dict__["indexed_item_count"] == 2
            assert records[0].levelno == logging.DEBUG

            caplog.clear()
            with caplog.at_level(logging.INFO, logger=logger_name):
                await backend.fit_async(items)
            assert not any(
                record.getMessage() == "Embedding index built with 2 items"
                for record in caplog.records
            )

            results = await backend.search_async("greeting", top_k=5)

            assert len(results) == 2
            # id1 should have higher similarity (identical embedding)
            assert results[0][0] == "id1"

    async def test_search_dimension_mismatch_raises(self) -> None:
        """A query dimension change must not degrade to an empty result."""
        backend = EmbeddingBackend()

        with (
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embeddings",
                new_callable=AsyncMock,
                return_value=[[1.0, 0.0]],
            ),
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embedding",
                new_callable=AsyncMock,
                return_value=[1.0, 0.0, 0.0],
            ),
        ):
            await backend.fit_async([("id1", "hello")])

            with pytest.raises(ValueError, match=r"Vector length mismatch: 2 != 3"):
                await backend.search_async("greeting")

    @pytest.mark.asyncio
    async def test_search_during_threaded_refit_uses_one_complete_index(self) -> None:
        backend = EmbeddingBackend()
        old_items = [("old-a", "old alpha"), ("old-b", "old beta")]
        new_items = [
            ("new-a", "new alpha"),
            ("new-b", "new beta"),
            ("new-c", "new gamma"),
        ]
        refit_started = threading.Event()
        release_refit = threading.Event()

        async def generate_embeddings(
            _service: object,
            contents: list[str],
        ) -> list[list[float]]:
            if contents == [content for _item_id, content in old_items]:
                return [[1.0, 0.0], [0.0, 1.0]]

            assert contents == [content for _item_id, content in new_items]
            refit_started.set()
            await asyncio.to_thread(release_refit.wait)
            return [[1.0, 0.0], [0.5, 0.5], [0.0, 1.0]]

        async def generate_query_embedding(
            _service: object,
            _query: str,
            **_kwargs: object,
        ) -> list[float]:
            return [1.0, 0.0]

        def refit_in_worker_thread() -> None:
            asyncio.run(backend.fit_async(new_items))

        with (
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embeddings",
                new=generate_embeddings,
            ),
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embedding",
                new=generate_query_embedding,
            ),
        ):
            await backend.fit_async(old_items)
            refit_task = asyncio.create_task(asyncio.to_thread(refit_in_worker_thread))
            try:
                assert await asyncio.to_thread(refit_started.wait, 5.0)
                during_refit = await backend.search_async("query", top_k=10)
            finally:
                release_refit.set()
                await refit_task

            after_refit = await backend.search_async("query", top_k=10)

        assert [item_id for item_id, _score in during_refit] == ["old-a"]
        assert [item_id for item_id, _score in after_refit] == ["new-a", "new-b"]

    @pytest.mark.asyncio
    async def test_normalized_scan_matches_cosine_and_preserves_ties(self) -> None:
        backend = EmbeddingBackend()
        item_embeddings = [
            [3.0, 4.0],
            [6.0, 8.0],
            [4.0, 3.0],
            [0.0, 0.0],
            [-3.0, -4.0],
        ]
        query_embedding = [3.0, 4.0]
        items = [(f"id{index}", str(index)) for index in range(len(item_embeddings))]

        with (
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embeddings",
                new_callable=AsyncMock,
                return_value=item_embeddings,
            ),
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embedding",
                new_callable=AsyncMock,
                return_value=query_embedding,
            ),
        ):
            await backend.fit_async(items)
            with patch.object(
                embedding_module,
                "_normalize_vector",
                wraps=embedding_module._normalize_vector,
            ) as normalize_query:
                results = await backend.search_async("query", top_k=10)

        expected: list[tuple[str, float]] = []
        for (item_id, _content), item_embedding in zip(items, item_embeddings, strict=True):
            similarity = cosine_similarity(query_embedding, item_embedding)
            if similarity > 0:
                expected.append((item_id, similarity))
        expected.sort(key=lambda result: result[1], reverse=True)

        assert [item_id for item_id, _score in results] == [item_id for item_id, _score in expected]
        assert [score for _item_id, score in results] == pytest.approx(
            [score for _item_id, score in expected]
        )
        assert normalize_query.call_count == 1
        assert results[0][0] == "id0"
        assert results[1][0] == "id1"
        normalized_norms = [
            math.sqrt(sum(value * value for value in vector)) for vector in backend._item_embeddings
        ]
        assert normalized_norms == pytest.approx([1.0, 1.0, 1.0, 0.0, 1.0])

    @pytest.mark.asyncio
    async def test_large_embedding_scan_runs_off_event_loop(self) -> None:
        backend = EmbeddingBackend()
        item_count = 400
        dimensions = 128
        items = [(f"id{index}", str(index)) for index in range(item_count)]
        item_embeddings = [
            [1.0, *([float(index % 2)] * (dimensions - 1))] for index in range(item_count)
        ]
        query_embedding = [1.0] * dimensions
        loop_thread = threading.get_ident()
        worker_threads: list[int] = []
        rank_embeddings = embedding_module._rank_embeddings
        rank_started = threading.Event()
        release_rank = threading.Event()

        def blocking_rank(
            item_ids: list[str],
            normalized_embeddings: list[list[float]],
            normalized_query: list[float],
            top_k: int,
        ) -> list[tuple[str, float]]:
            worker_threads.append(threading.get_ident())
            rank_started.set()
            if not release_rank.wait(timeout=2):
                raise TimeoutError("test did not release embedding rank")
            return rank_embeddings(
                item_ids,
                normalized_embeddings,
                normalized_query,
                top_k,
            )

        with (
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embeddings",
                new_callable=AsyncMock,
                return_value=item_embeddings,
            ),
            patch(
                "gobby.search.backends.embedding.EmbeddingService.generate_embedding",
                new_callable=AsyncMock,
                return_value=query_embedding,
            ),
        ):
            await backend.fit_async(items)
            with patch.object(embedding_module, "_rank_embeddings", side_effect=blocking_rank):
                search_task = asyncio.create_task(backend.search_async("query", top_k=10))
                try:
                    started = await asyncio.to_thread(rank_started.wait, 2)
                    assert started, "embedding rank did not start"
                    assert not search_task.done(), "embedding scan stalled the event loop"
                finally:
                    release_rank.set()

                results = await search_task

        assert len(results) == 10
        assert len(worker_threads) == 1
        assert worker_threads[0] != loop_thread

    @pytest.mark.asyncio
    async def test_fit_dimension_mismatch_raises(self) -> None:
        """Backend fit should fail fast when provider output has the wrong dimension."""
        backend = EmbeddingBackend(
            model="dimension-mismatch-model",
            api_base="http://localhost:1234/v1",
            dim=4,
        )
        mock_client = _make_openai_client(dim=3)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            with pytest.raises(EmbeddingGenerationError, match="expected_dim=4"):
                await backend.fit_async([("id1", "hello")])

    @pytest.mark.asyncio
    async def test_empty_fit(self) -> None:
        """Test fitting with empty items."""
        backend = EmbeddingBackend()
        await backend.fit_async([])

        assert backend._fitted
        assert not backend.needs_refit()
        results = await backend.search_async("test")
        assert results == []

    @pytest.mark.parametrize(
        "updated_items",
        [
            [("id1", "alpha"), ("id2", "beta")],
            [("id1", "alpha updated")],
            [],
        ],
    )
    @pytest.mark.asyncio
    async def test_mark_update_needs_refit_until_next_fit(
        self,
        updated_items: list[tuple[str, str]],
    ) -> None:
        backend = EmbeddingBackend()

        async def fit_vectors(
            texts: list[str],
            **_: object,
        ) -> list[list[float]]:
            return [[float(index + 1)] for index, _text in enumerate(texts)]

        with patch(
            "gobby.search.backends.embedding.EmbeddingService.generate_embeddings",
            side_effect=fit_vectors,
        ):
            await backend.fit_async([("id1", "alpha")])
            assert not backend.needs_refit()

            backend.mark_update()

            assert backend.needs_refit()
            await backend.fit_async(updated_items)
            assert not backend.needs_refit()

    def test_get_stats(self) -> None:
        """Test get_stats returns expected keys."""
        backend = EmbeddingBackend(model="test-model")
        stats = backend.get_stats()

        assert stats["backend_type"] == "embedding"
        assert stats["model"] == "test-model"
        assert "fitted" in stats
        assert "item_count" in stats

    def test_clear(self) -> None:
        """Test clear resets state."""
        backend = EmbeddingBackend()
        backend._item_ids = ["id1", "id2"]
        backend._fitted = True

        backend.clear()

        assert backend._item_ids == []
        assert not backend._fitted
