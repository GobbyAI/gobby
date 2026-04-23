"""
Unified search backend abstraction.

Provides a unified search layer with multiple backends:
- FTS5 (default) - SQLite full-text search, always available
- Embedding - OpenAI-compatible semantic search (OpenAI, Ollama, etc.)
- Unified - Orchestrates between backends with automatic fallback

Basic usage (FTS5):
    from gobby.search import FTS5SearchBackend

    backend = FTS5SearchBackend(db, "tasks_fts", "tasks", weights=(10.0, 5.0))
    results = await backend.search_async("query text", top_k=10)

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
    generate_embedding,
    generate_embeddings,
    is_embedding_configured,
    is_embedding_reachable,
)

# FTS5 backend
from gobby.search.fts5 import FTS5SearchBackend, sanitize_fts_query
from gobby.search.models import FallbackEvent, SearchConfig, SearchMode

# Unified search (async with fallback)
from gobby.search.unified import UnifiedSearcher

__all__ = [
    # Async backends
    "AsyncSearchBackend",
    "EmbeddingBackend",
    # FTS5 backend
    "FTS5SearchBackend",
    "sanitize_fts_query",
    # Models
    "SearchConfig",
    "SearchMode",
    "FallbackEvent",
    # Unified searcher
    "UnifiedSearcher",
    # Embedding utilities
    "generate_embedding",
    "generate_embeddings",
    "is_embedding_configured",
    "is_embedding_reachable",
]
