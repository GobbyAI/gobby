"""Tests for the unified search module."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gobby.search import (
    EmbeddingBackend,
    EmbeddingGenerationError,
    FallbackEvent,
    SearchConfig,
    SearchMode,
    UnifiedSearcher,
)
from gobby.search.keyword import KeywordAsyncSearchBackend

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

    @dataclass
    class FakeResponse:
        data: list[FakeItem]

    async def fake_create(model: str, input: list[str]) -> FakeResponse:
        return FakeResponse([FakeItem([0.1] * dim) for _ in input])

    create_mock: AsyncMock = AsyncMock(side_effect=fake_create)
    mock_client.embeddings.create = create_mock
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
            "gobby.search.unified.is_embedding_reachable",
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

    @pytest.mark.asyncio
    async def test_auto_mode_embedding_available(self, db) -> None:
        """Test auto mode uses embedding when available."""
        config = SearchConfig(mode="auto")

        mock_embeddings = [[0.1, 0.2, 0.3]] * 2

        with (
            patch(
                "gobby.search.unified.is_embedding_reachable",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "gobby.search.embeddings.generate_embeddings",
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
                "gobby.search.unified.is_embedding_reachable",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "gobby.search.embeddings.generate_embeddings",
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

        with patch("gobby.search.unified.is_embedding_configured", return_value=False):
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
            patch("gobby.search.unified.is_embedding_configured", return_value=True),
            patch(
                "gobby.search.unified.is_embedding_reachable",
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
            patch("gobby.search.unified.is_embedding_configured", return_value=True),
            patch(
                "gobby.search.embeddings.generate_embeddings",
                new_callable=AsyncMock,
                return_value=mock_embeddings,
            ),
            patch(
                "gobby.search.embeddings.generate_embedding",
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
            "gobby.search.unified.is_embedding_reachable",
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
            patch("gobby.search.unified.is_embedding_configured", return_value=True),
            patch(
                "gobby.search.embeddings.generate_embeddings",
                new_callable=AsyncMock,
                side_effect=RuntimeError("API error"),
            ),
        ):
            searcher = _make_searcher(db, config)
            await searcher.fit_async([("id1", "test")])

            assert searcher.get_active_backend() == "keyword"


class TestDeprecatedSqliteKeywordSearch:
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
    async def test_fit_and_search(self) -> None:
        """Test fit and search with mocked embeddings."""
        backend = EmbeddingBackend()

        mock_fit_embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        mock_query_embedding = [0.1, 0.2, 0.3]  # Similar to first item

        with (
            patch(
                "gobby.search.embeddings.generate_embeddings",
                new_callable=AsyncMock,
                return_value=mock_fit_embeddings,
            ),
            patch(
                "gobby.search.embeddings.generate_embedding",
                new_callable=AsyncMock,
                return_value=mock_query_embedding,
            ),
        ):
            items = [("id1", "hello"), ("id2", "world")]
            await backend.fit_async(items)

            results = await backend.search_async("greeting", top_k=5)

            assert len(results) == 2
            # id1 should have higher similarity (identical embedding)
            assert results[0][0] == "id1"

    @pytest.mark.asyncio
    async def test_fit_dimension_mismatch_raises(self) -> None:
        """Backend fit should fail fast when provider output has the wrong dimension."""
        backend = EmbeddingBackend(model="dimension-mismatch-model", dim=4)
        mock_client = _make_openai_client(dim=3)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            with pytest.raises(EmbeddingGenerationError, match="expected_dim=4"):
                await backend.fit_async([("id1", "hello")])

    @pytest.mark.asyncio
    async def test_empty_fit(self) -> None:
        """Test fitting with empty items."""
        backend = EmbeddingBackend()
        await backend.fit_async([])

        assert not backend._fitted
        results = await backend.search_async("test")
        assert results == []

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
