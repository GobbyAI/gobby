"""Embedding-based search backend.

This module provides embedding-based semantic search using cosine similarity.
It stores embeddings in memory and uses an OpenAI-compatible embeddings API.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from typing import TYPE_CHECKING, Any

from gobby.ai.embeddings import EmbeddingService
from gobby.search.similarity import cosine_similarity as _cosine_similarity

if TYPE_CHECKING:
    from gobby.config.persistence import EmbeddingsConfig

logger = logging.getLogger(__name__)


_SCAN_OFFLOAD_MIN_FLOAT_OPS = 50_000


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
            api_key="sk-...",
            dim=1536,
        )
        await backend.fit_async([("id1", "hello"), ("id2", "world")])
        results = await backend.search_async("greeting", top_k=5)
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        api_base: str | None = None,
        api_key: str | None = None,
        dim: int | None = None,
    ):
        """Initialize embedding backend.

        Args:
            model: Embedding model name exposed by the target endpoint
            api_base: Optional API base URL for custom endpoints
            api_key: Optional API key (uses env var if not set)
            dim: Expected embedding dimension. When set, mismatches fail fast.
        """
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._dim = dim
        self._embedding_service = EmbeddingService(
            model=model,
            api_base=api_base,
            api_key=api_key,
            dim=dim,
        )

        # Item storage
        self._item_ids: list[str] = []
        self._item_embeddings: list[list[float]] = []
        self._item_contents: dict[str, str] = {}  # For reindexing
        self._fitted = False
        self._needs_refit = False
        self._state_lock = threading.Lock()

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
            dim=config.dim,
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
            with self._state_lock:
                self._item_ids = []
                self._item_embeddings = []
                self._item_contents = {}
                self._fitted = True
                self._needs_refit = False
            logger.debug("Embedding index cleared (no items)")
            return

        item_ids = [item_id for item_id, _ in items]
        item_contents = dict(items)
        contents = [content for _, content in items]

        # Generate embeddings in batch
        try:
            embeddings = await self._embedding_service.generate_embeddings(contents)
            if _embedding_float_count(embeddings) >= _SCAN_OFFLOAD_MIN_FLOAT_OPS:
                item_embeddings = await asyncio.to_thread(_normalize_vectors, embeddings)
            else:
                item_embeddings = _normalize_vectors(embeddings)
            with self._state_lock:
                self._item_ids = item_ids
                self._item_contents = item_contents
                self._item_embeddings = item_embeddings
                self._fitted = True
                self._needs_refit = False
            logger.debug(
                "Embedding index built",
                extra={"indexed_item_count": len(items)},
            )
        except Exception as e:
            # Clear stale state to prevent inconsistent data
            with self._state_lock:
                self._item_ids = []
                self._item_contents = {}
                self._item_embeddings = []
                self._fitted = False
                self._needs_refit = False
            logger.error("Failed to build embedding index: %s", e)
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
        with self._state_lock:
            if not self._fitted or not self._item_embeddings:
                return []
            item_ids = self._item_ids
            item_embeddings = self._item_embeddings

        # Generate query embedding
        try:
            query_embedding = await self._embedding_service.generate_embedding(
                query,
                is_query=True,
            )
        except Exception as e:
            logger.error("Failed to embed query: %s", e)
            raise

        normalized_query = _normalize_vector(query_embedding)
        scan_float_ops = len(item_embeddings) * len(normalized_query)
        if scan_float_ops >= _SCAN_OFFLOAD_MIN_FLOAT_OPS:
            return await asyncio.to_thread(
                _rank_embeddings,
                item_ids,
                item_embeddings,
                normalized_query,
                top_k,
            )
        return _rank_embeddings(
            item_ids,
            item_embeddings,
            normalized_query,
            top_k,
        )

    def needs_refit(self) -> bool:
        """Check if the search index needs rebuilding."""
        with self._state_lock:
            return not self._fitted or self._needs_refit

    def mark_update(self) -> None:
        """Mark that indexed item data changed after the last fit."""
        with self._state_lock:
            self._needs_refit = True

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the search index."""
        with self._state_lock:
            return {
                "backend_type": "embedding",
                "fitted": self._fitted,
                "item_count": len(self._item_ids),
                "model": self._model,
                "has_api_base": self._api_base is not None,
            }

    def clear(self) -> None:
        """Clear the search index."""
        with self._state_lock:
            self._item_ids = []
            self._item_embeddings = []
            self._item_contents = {}
            self._fitted = False
            self._needs_refit = False

    def get_item_contents(self) -> dict[str, str]:
        """Get stored item contents.

        Useful for reindexing into a different backend (e.g., keyword fallback).

        Returns:
            Dict mapping item_id to content
        """
        with self._state_lock:
            return self._item_contents.copy()


def _embedding_float_count(embeddings: list[list[float]]) -> int:
    return sum(len(embedding) for embedding in embeddings)


def _normalize_vectors(embeddings: list[list[float]]) -> list[list[float]]:
    return [_normalize_vector(embedding) for embedding in embeddings]


def _normalize_vector(embedding: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in embedding))
    if norm == 0:
        return [0.0] * len(embedding)
    return [value / norm for value in embedding]


def _rank_embeddings(
    item_ids: list[str],
    normalized_embeddings: list[list[float]],
    normalized_query: list[float],
    top_k: int,
) -> list[tuple[str, float]]:
    similarities: list[tuple[str, float]] = []
    for item_id, item_embedding in zip(item_ids, normalized_embeddings, strict=True):
        if len(item_embedding) != len(normalized_query):
            _cosine_similarity(item_embedding, normalized_query)
        similarity = sum(
            query_value * item_value
            for query_value, item_value in zip(normalized_query, item_embedding, strict=True)
        )
        if similarity > 0:
            similarities.append((item_id, similarity))
    similarities.sort(key=lambda result: result[1], reverse=True)
    return similarities[:top_k]
