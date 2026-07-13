"""Focused coverage for hybrid-search rank fusion."""

from __future__ import annotations

import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.search import SearchConfig, SearchMode, UnifiedSearcher


def _make_fitted_hybrid_searcher(
    config: SearchConfig | None = None,
) -> tuple[UnifiedSearcher, AsyncMock, AsyncMock]:
    searcher = UnifiedSearcher(
        config or SearchConfig(mode="hybrid"),
        db=SimpleNamespace(dialect="postgres"),
        fts_table="skills_fts",
    )
    keyword_search = AsyncMock()
    embedding_search = AsyncMock()
    keyword_backend = MagicMock()
    embedding_backend = MagicMock()
    keyword_backend.search_async = keyword_search
    embedding_backend.search_async = embedding_search
    searcher._keyword_backend = keyword_backend
    searcher._embedding_backend = embedding_backend
    searcher._fitted = True
    searcher._fitted_mode = SearchMode.HYBRID
    searcher._active_backend = "hybrid"
    return searcher, keyword_search, embedding_search


@pytest.mark.asyncio
async def test_hybrid_rank_fusion_uses_comparable_backend_ranks() -> None:
    config = SearchConfig(mode="hybrid", keyword_weight=0.5, embedding_weight=0.5)
    searcher, keyword_search, embedding_search = _make_fitted_hybrid_searcher(config)
    keyword_search.return_value = [
        ("keyword-only", 1.0),
        ("shared", 0.0001),
    ]
    embedding_search.return_value = [
        ("shared", 0.99),
        ("embedding-only", 0.98),
    ]

    results = await searcher.search_async("query", top_k=3)

    assert [item_id for item_id, _score in results] == [
        "shared",
        "keyword-only",
        "embedding-only",
    ]
    assert all(0.0 <= score <= 1.0 for _item_id, score in results)


@pytest.mark.asyncio
async def test_hybrid_rank_fusion_reweights_when_embedding_degrades() -> None:
    config = SearchConfig(
        mode="hybrid",
        keyword_weight=0.1,
        embedding_weight=0.9,
        notify_on_fallback=False,
    )
    searcher, keyword_search, embedding_search = _make_fitted_hybrid_searcher(config)
    keyword_search.return_value = [("keyword-first", 1.0), ("keyword-second", 0.5)]
    embedding_search.side_effect = RuntimeError("embedding unavailable")

    results = await searcher.search_async("query", top_k=2)

    assert results[0] == ("keyword-first", 1.0)
    assert [item_id for item_id, _score in results] == [
        "keyword-first",
        "keyword-second",
    ]
    assert all(0.0 <= score <= 1.0 for _item_id, score in results)


@pytest.mark.asyncio
async def test_hybrid_equal_scores_use_id_tiebreak_before_truncation() -> None:
    config = SearchConfig(mode="hybrid", keyword_weight=0.5, embedding_weight=0.5)
    searcher, keyword_search, embedding_search = _make_fitted_hybrid_searcher(config)
    item_ids = ["id-a", "id-b"]
    rng = random.Random(16278)

    for _iteration in range(20):
        rng.shuffle(item_ids)
        keyword_search.return_value = [
            (item_ids[0], 1.0),
            (item_ids[1], 0.99),
        ]
        embedding_search.return_value = [(item_id, 0.2) for item_id in reversed(item_ids)]

        ranked = await searcher.search_async("query", top_k=2)
        truncated = await searcher.search_async("query", top_k=1)

        assert [item_id for item_id, _score in ranked] == ["id-a", "id-b"]
        assert ranked[0][1] == pytest.approx(ranked[1][1])
        assert [item_id for item_id, _score in truncated] == ["id-a"]


@pytest.mark.asyncio
async def test_hybrid_semantic_hit_beats_trivial_keyword_match() -> None:
    searcher, keyword_search, embedding_search = _make_fitted_hybrid_searcher()
    keyword_search.return_value = [("trivial-keyword", 1.0)]
    embedding_search.return_value = [("semantic-hit", 0.6)]

    results = await searcher.search_async("query", top_k=2)

    assert [item_id for item_id, _score in results] == [
        "semantic-hit",
        "trivial-keyword",
    ]
    assert all(0.0 <= score <= 1.0 for _item_id, score in results)
