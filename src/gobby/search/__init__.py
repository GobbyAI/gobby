"""
Unified search backend abstraction.

Provides a unified search layer with multiple backends:
- TF-IDF (default) - Built-in local search using scikit-learn
- Embedding - LiteLLM-based semantic search (OpenAI, Ollama, etc.)
- Unified - Orchestrates between backends with automatic fallback

Basic usage (sync TF-IDF):
    from gobby.search import TFIDFSearcher

    backend = TFIDFSearcher()
    backend.fit([(id, content) for id, content in items])
    results = backend.search("query text", top_k=10)

Unified search (async with fallback):
    from gobby.search import UnifiedSearcher, SearchConfig

    config = SearchConfig(mode="auto")  # auto, tfidf, embedding, hybrid
    searcher = UnifiedSearcher(config)
    await searcher.fit_async([(id, content) for id, content in items])
    results = await searcher.search_async("query text", top_k=10)

    if searcher.is_using_fallback():
        print(f"Using fallback: {searcher.get_fallback_reason()}")
"""

# Sync search backends (backwards compatibility)
# Async backends
from gobby.search.backends import AsyncSearchBackend, EmbeddingBackend, TFIDFBackend

# Embedding utilities
from gobby.search.embeddings import (
    generate_embedding,
    generate_embeddings,
    is_embedding_available,
)
from gobby.search.models import FallbackEvent, SearchConfig, SearchMode
from gobby.search.protocol import SearchBackend, SearchResult, get_search_backend
from gobby.search.tfidf import TFIDFSearcher

# Unified search (async with fallback)
from gobby.search.unified import UnifiedSearcher

__all__ = [
    # Sync backends (backwards compatible)
    "SearchBackend",
    "SearchResult",
    "TFIDFSearcher",
    "get_search_backend",
    # Models
    "SearchConfig",
    "SearchMode",
    "FallbackEvent",
    # Unified searcher
    "UnifiedSearcher",
    # Async backends
    "AsyncSearchBackend",
    "TFIDFBackend",
    "EmbeddingBackend",
    # Embedding utilities
    "generate_embedding",
    "generate_embeddings",
    "is_embedding_available",
]
