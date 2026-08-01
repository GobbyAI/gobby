"""
Unified search backend abstraction.

Provides a unified search layer with multiple backends:
- Keyword - PostgreSQL pg_search BM25
- Embedding - OpenAI-compatible semantic search (OpenAI, Ollama, etc.)
- Unified - Orchestrates between backends with automatic fallback

Basic usage (keyword):
    from gobby.search import SearchConfig, UnifiedSearcher

    searcher = UnifiedSearcher(SearchConfig(mode="keyword"), db=db, fts_table="tasks")
    results = await searcher.search_async("query text", top_k=10)

Unified search (async with fallback):
    from gobby.search import UnifiedSearcher, SearchConfig

    config = SearchConfig(mode="auto")
    searcher = UnifiedSearcher(config, db=db, fts_table="skills_fts", ...)
    await searcher.fit_async([(id, content) for id, content in items])
    results = await searcher.search_async("query text", top_k=10)

    if searcher.is_using_fallback():
        print(f"Using fallback: {searcher.get_fallback_reason()}")
"""

from typing import Any

from gobby.search.keyword import BM25SearchBackend, KeywordSearchBackend, SearchHit
from gobby.search.models import FallbackEvent, SearchConfig, SearchMode

__all__ = [
    # Async backends
    "AsyncSearchBackend",
    "EmbeddingBackend",
    # Keyword backend
    "BM25SearchBackend",
    "KeywordSearchBackend",
    "SearchHit",
    # Models
    "SearchConfig",
    "SearchMode",
    "FallbackEvent",
    # Unified searcher
    "UnifiedSearcher",
]


def __getattr__(name: str) -> Any:
    """Load backend and unified search exports lazily to avoid config import cycles."""
    if name == "UnifiedSearcher":
        from gobby.search.unified import UnifiedSearcher

        return UnifiedSearcher
    if name in {"AsyncSearchBackend", "EmbeddingBackend"}:
        from gobby.search.backends import AsyncSearchBackend, EmbeddingBackend

        return {
            "AsyncSearchBackend": AsyncSearchBackend,
            "EmbeddingBackend": EmbeddingBackend,
        }[name]
    raise AttributeError(f"module 'gobby.search' has no attribute {name!r}")
