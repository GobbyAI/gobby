"""Search backend abstractions.

This module provides the protocol and implementations for search backends
used by UnifiedSearcher:

- AsyncSearchBackend: Protocol for async search backends
- EmbeddingBackend: Embedding-based search (requires API)

Usage:
    from gobby.search.backends import AsyncSearchBackend
    from gobby.search.keyword import KeywordAsyncSearchBackend

    backend: AsyncSearchBackend = KeywordAsyncSearchBackend(db, "tasks")
    results = await backend.search_async("query", top_k=10)
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from gobby.search.backends.embedding import EmbeddingBackend

__all__ = [
    "AsyncSearchBackend",
    "EmbeddingBackend",
]


@runtime_checkable
class AsyncSearchBackend(Protocol):
    """Protocol for async search backends.

    All search backends must implement this interface. The protocol
    uses async methods to support embedding-based backends that need
    to call external APIs.

    Methods:
        fit_async: Build/rebuild the search index
        search_async: Find relevant items for a query
        needs_refit: Check if index needs rebuilding
        get_stats: Get backend statistics
        clear: Clear the search index
    """

    async def fit_async(self, items: list[tuple[str, str]]) -> None:
        """Build or rebuild the search index.

        Args:
            items: List of (item_id, content) tuples to index
        """
        ...

    async def search_async(
        self,
        query: str,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Search for items matching the query.

        Args:
            query: Search query text
            top_k: Maximum number of results to return

        Returns:
            List of (item_id, similarity_score) tuples, sorted by
            relevance (highest similarity first).
        """
        ...

    def needs_refit(self) -> bool:
        """Check if the search index needs rebuilding.

        Returns:
            True if fit_async() should be called before search_async()
        """
        ...

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the search index.

        Returns:
            Dict with backend-specific statistics
        """
        ...

    def clear(self) -> None:
        """Clear the search index."""
        ...
