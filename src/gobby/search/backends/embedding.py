"""Embedding-based search backend.

This module provides embedding-based semantic search using cosine similarity.
It stores embeddings in memory and uses an OpenAI-compatible embeddings API.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.config.persistence import EmbeddingsConfig

logger = logging.getLogger(__name__)


def _cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        vec1: First vector
        vec2: Second vector

    Returns:
        Cosine similarity score between -1 and 1
    """
    if len(vec1) != len(vec2):
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec1, vec2, strict=True))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)


class EmbeddingBackend:
    """Embedding-based search backend using an OpenAI-compatible API.

    This backend generates embeddings for indexed items and uses
    cosine similarity for search. Embeddings are stored in memory.

    Supports OpenAI-compatible providers such as:
    - OpenAI (text-embedding-3-small)
    - Ollama (nomic-embed-text with api_base)
    - LM Studio and other local routers

    Example:
        backend = EmbeddingBackend(
            model="text-embedding-3-small",
            api_key="sk-..."
        )
        await backend.fit_async([("id1", "hello"), ("id2", "world")])
        results = await backend.search_async("greeting", top_k=5)
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        api_base: str | None = None,
        api_key: str | None = None,
    ):
        """Initialize embedding backend.

        Args:
            model: Embedding model name exposed by the target endpoint
            api_base: Optional API base URL for custom endpoints
            api_key: Optional API key (uses env var if not set)
        """
        self._model = model
        self._api_base = api_base
        self._api_key = api_key

        # Item storage
        self._item_ids: list[str] = []
        self._item_embeddings: list[list[float]] = []
        self._item_contents: dict[str, str] = {}  # For reindexing
        self._fitted = False

    @classmethod
    def from_config(cls, config: EmbeddingsConfig) -> EmbeddingBackend:
        """Create an EmbeddingBackend from an EmbeddingsConfig.

        Args:
            config: EmbeddingsConfig with model and API settings

        Returns:
            Configured EmbeddingBackend instance
        """
        return cls(
            model=config.model,
            api_base=config.api_base,
            api_key=config.api_key,
        )

    async def fit_async(self, items: list[tuple[str, str]]) -> None:
        """Build or rebuild the search index.

        Generates embeddings for all items and stores them in memory.

        Args:
            items: List of (item_id, content) tuples to index

        Raises:
            RuntimeError: If embedding generation fails
        """
        if not items:
            self._item_ids = []
            self._item_embeddings = []
            self._item_contents = {}
            self._fitted = False
            logger.debug("Embedding index cleared (no items)")
            return

        from gobby.search.embeddings import generate_embeddings

        # Store contents for potential reindexing
        self._item_ids = [item_id for item_id, _ in items]
        self._item_contents = dict(items)
        contents = [content for _, content in items]

        # Generate embeddings in batch
        try:
            self._item_embeddings = await generate_embeddings(
                texts=contents,
                model=self._model,
                api_base=self._api_base,
                api_key=self._api_key,
            )
            self._fitted = True
            logger.info(f"Embedding index built with {len(items)} items")
        except Exception as e:
            # Clear stale state to prevent inconsistent data
            self._item_ids = []
            self._item_contents = {}
            self._item_embeddings = []
            self._fitted = False
            logger.error(f"Failed to build embedding index: {e}")
            raise

    async def search_async(
        self,
        query: str,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Search for items matching the query.

        Generates an embedding for the query and finds items with
        highest cosine similarity.

        Args:
            query: Search query text
            top_k: Maximum number of results to return

        Returns:
            List of (item_id, similarity_score) tuples, sorted by
            similarity descending.

        Raises:
            RuntimeError: If embedding generation fails
        """
        if not self._fitted or not self._item_embeddings:
            return []

        from gobby.search.embeddings import generate_embedding

        # Generate query embedding
        try:
            query_embedding = await generate_embedding(
                text=query,
                model=self._model,
                api_base=self._api_base,
                api_key=self._api_key,
                is_query=True,
            )
        except Exception as e:
            logger.error(f"Failed to embed query: {e}")
            raise

        # Compute similarities
        similarities: list[tuple[str, float]] = []
        for item_id, item_embedding in zip(self._item_ids, self._item_embeddings, strict=True):
            similarity = _cosine_similarity(query_embedding, item_embedding)
            if similarity > 0:
                similarities.append((item_id, similarity))

        # Sort by similarity descending
        similarities.sort(key=lambda x: x[1], reverse=True)

        return similarities[:top_k]

    def needs_refit(self) -> bool:
        """Check if the search index needs rebuilding."""
        return not self._fitted

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the search index."""
        return {
            "backend_type": "embedding",
            "fitted": self._fitted,
            "item_count": len(self._item_ids),
            "model": self._model,
            "has_api_base": self._api_base is not None,
        }

    def clear(self) -> None:
        """Clear the search index."""
        self._item_ids = []
        self._item_embeddings = []
        self._item_contents = {}
        self._fitted = False

    def get_item_contents(self) -> dict[str, str]:
        """Get stored item contents.

        Useful for reindexing into a different backend (e.g., keyword fallback).

        Returns:
            Dict mapping item_id to content
        """
        return self._item_contents.copy()
