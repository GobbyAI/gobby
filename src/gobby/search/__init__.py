"""
Unified search backend abstraction.

Provides a unified search layer with multiple backends:
- Keyword - PostgreSQL pg_search BM25
- Embedding - OpenAI-compatible semantic search (OpenAI, Ollama, etc.)
- Unified - Orchestrates between backends with automatic fallback

Basic usage (keyword):
    from gobby.search import SearchConfig, UnifiedSearcher

    searcher = UnifiedSearcher(SearchConfig(mode="keyword"), db=db, fts_table="tasks_fts")
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

# Async backends
from gobby.search.backends import AsyncSearchBackend, EmbeddingBackend

# Embedding utilities
from gobby.search.embeddings import (
    EmbeddingGenerationError,
    generate_embedding,
    generate_embeddings,
    is_embedding_configured,
    is_embedding_reachable,
)
from gobby.search.keyword import BM25SearchBackend, KeywordSearchBackend, SearchHit
from gobby.search.models import FallbackEvent, SearchConfig, SearchMode

# Unified search (async with fallback)
from gobby.search.unified import UnifiedSearcher

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
    # Embedding utilities
    "EmbeddingGenerationError",
    "generate_embedding",
    "generate_embeddings",
    "is_embedding_configured",
    "is_embedding_reachable",
]
