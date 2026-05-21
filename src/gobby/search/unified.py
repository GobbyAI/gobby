"""Unified search orchestration with fallback.

This module provides UnifiedSearcher, the main entry point for the unified
search layer. It orchestrates between embedding-based and keyword backends
with automatic fallback and configurable search modes.

Example usage:
    from gobby.search.unified import UnifiedSearcher
    from gobby.search.models import SearchConfig

    config = SearchConfig(mode="auto")
    searcher = UnifiedSearcher(config, embedding_model="nomic-embed-text")

    await searcher.fit_async([
        ("id1", "hello world"),
        ("id2", "foo bar"),
    ])

    results = await searcher.search_async("greeting", top_k=5)
    # Returns: [("id1", 0.85), ...]
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from gobby.search.backends import AsyncSearchBackend, EmbeddingBackend
from gobby.search.embeddings import (
    is_embedding_configured,
    is_embedding_reachable,
)
from gobby.search.keyword import KeywordAsyncSearchBackend, keyword_table_for_fts_table
from gobby.search.models import FallbackEvent, SearchConfig, SearchMode

logger = logging.getLogger(__name__)

# Type alias for fallback event callback
FallbackCallback = Callable[[FallbackEvent], None]


class UnifiedSearcher:
    """Unified search with automatic fallback.

    Orchestrates between embedding-based and keyword search backends
    based on the configured mode and availability of embedding providers.

    Search Modes:
    - keyword: PostgreSQL keyword search only
    - embedding: Embedding-based only (fails if unavailable)
    - auto: Try embedding, fallback to keyword if unavailable
    - hybrid: Combine both with weighted scores

    Fallback Behavior:
    When in "auto" mode and embedding fails (no API key, connection error,
    rate limit), the searcher will:
    1. Emit a FallbackEvent via the event_callback
    2. Log a warning (if notify_on_fallback is True)
    3. Return keyword results for this and future searches

    Example:
        config = SearchConfig(mode="auto")
        searcher = UnifiedSearcher(
            config, db=db, fts_table="skills_fts",
            fts_content_table="skills", fts_weights=(10.0, 5.0, 2.0, 2.0),
            event_callback=lambda e: print(f"Fallback: {e}")
        )

        await searcher.fit_async([("id1", "content1")])
        results = await searcher.search_async("query")

        if searcher.is_using_fallback():
            print("Using keyword fallback")
    """

    def __init__(
        self,
        config: SearchConfig | None = None,
        event_callback: FallbackCallback | None = None,
        *,
        db: Any,
        fts_table: str,
        fts_content_table: str | None = None,
        fts_id_column: str = "id",
        fts_weights: tuple[float, ...] | None = None,
        embedding_model: str = "nomic-embed-text",
        embedding_api_base: str | None = None,
        embedding_api_key: str | None = None,
        embedding_dim: int | None = None,
    ):
        """Initialize UnifiedSearcher.

        Args:
            config: Search configuration (defaults to SearchConfig())
            event_callback: Optional callback for fallback events
            db: Hub database instance for keyword search (required)
            fts_table: Legacy keyword table alias (required)
            fts_content_table: Retained for compatibility; ignored by the keyword backend
            fts_id_column: ID column name in the content table
            fts_weights: Retained for compatibility; ignored by the keyword backend
            embedding_model: Embedding model name (from EmbeddingsConfig)
            embedding_api_base: API base URL for embedding endpoint
            embedding_api_key: API key for embedding provider
            embedding_dim: Expected embedding dimension. When set, mismatches fail fast.
        """
        self._config = config or SearchConfig()
        self._event_callback = event_callback
        self._embedding_model = embedding_model
        self._embedding_api_base = embedding_api_base
        self._embedding_api_key = embedding_api_key
        self._embedding_dim = embedding_dim

        # Legacy keyword config
        self._db = db
        self._fts_table = fts_table
        self._fts_content_table = fts_content_table
        self._fts_id_column = fts_id_column
        self._fts_weights = fts_weights

        # Initialize backends lazily
        self._keyword_backend: AsyncSearchBackend | None = None
        self._embedding_backend: EmbeddingBackend | None = None

        # State tracking
        self._items: list[tuple[str, str]] = []  # Cache for reindexing
        self._fitted = False
        self._fitted_mode: SearchMode | None = None  # Track mode used during fit
        self._using_fallback = False
        self._fallback_reason: str | None = None
        self._active_backend: str | None = None

    @property
    def config(self) -> SearchConfig:
        """Get the current configuration."""
        return self._config

    def _get_keyword_backend(self) -> AsyncSearchBackend:
        """Get or create the dialect-aware keyword search backend."""
        if self._keyword_backend is None:
            self._keyword_backend = KeywordAsyncSearchBackend(
                self._db,
                keyword_table_for_fts_table(self._fts_table),
            )
        return self._keyword_backend

    def _get_embedding_backend(self) -> EmbeddingBackend:
        """Get or create the embedding backend."""
        if self._embedding_backend is None:
            self._embedding_backend = EmbeddingBackend(
                model=self._embedding_model,
                api_base=self._embedding_api_base,
                api_key=self._embedding_api_key,
                dim=self._embedding_dim,
            )
        return self._embedding_backend

    def _emit_fallback_event(
        self,
        reason: str,
        error: Exception | None = None,
        items_reindexed: int = 0,
    ) -> None:
        """Emit a fallback event and log if configured."""
        event = FallbackEvent(
            reason=reason,
            original_error=error,
            mode=self._config.mode,
            items_reindexed=items_reindexed,
        )

        # Log warning if configured
        if self._config.notify_on_fallback:
            logger.warning(f"Search fallback: {reason}")

        # Call event callback if provided
        if self._event_callback:
            try:
                self._event_callback(event)
            except Exception as e:
                logger.error(f"Fallback callback error: {e}")

    async def _fallback_to_keyword(
        self,
        reason: str,
        error: Exception | None = None,
        items: list[tuple[str, str]] | None = None,
    ) -> None:
        """Switch to keyword backend and reindex.

        Args:
            reason: Human-readable reason for fallback
            error: Optional exception that caused the fallback
            items: Items to index. If None, uses cached self._items
        """
        self._using_fallback = True
        self._fallback_reason = reason
        self._active_backend = "keyword"

        # Fit keyword backend with provided items or cached items
        fit_items = items if items is not None else self._items
        items_reindexed = 0
        if fit_items:
            keyword = self._get_keyword_backend()
            await keyword.fit_async(fit_items)
            items_reindexed = len(fit_items)
            self._fitted = True
            self._fitted_mode = SearchMode.KEYWORD

        self._emit_fallback_event(reason, error, items_reindexed)

    async def fit_async(self, items: list[tuple[str, str]]) -> None:
        """Build or rebuild the search index.

        Indexes items into the appropriate backend(s) based on mode:
        - keyword: keyword only
        - embedding: Embedding only (raises if unavailable)
        - auto: Try embedding, fallback to keyword if unavailable
        - hybrid: Both keyword and embedding

        Args:
            items: List of (item_id, content) tuples to index

        Raises:
            RuntimeError: If mode is "embedding" and embedding unavailable
        """
        self._items = items.copy()
        self._fitted = False
        self._fitted_mode = None
        mode = self._config.get_mode_enum()

        if mode == SearchMode.KEYWORD:
            # Keyword only
            keyword = self._get_keyword_backend()
            await keyword.fit_async(items)
            self._active_backend = "keyword"
            self._fitted = True
            self._fitted_mode = mode

        elif mode == SearchMode.EMBEDDING:
            # Embedding only - fail if unavailable. Check config first so we
            # can distinguish "not configured" from "configured but unreachable"
            # in the error message; then actually probe the endpoint.
            if not is_embedding_configured(
                model=self._embedding_model,
                api_key=self._embedding_api_key,
                api_base=self._embedding_api_base,
            ):
                raise RuntimeError(
                    f"Embedding not configured for model {self._embedding_model}. "
                    "Set the appropriate API key or api_base, or use mode='auto' "
                    "for fallback."
                )
            if not await is_embedding_reachable(
                model=self._embedding_model,
                api_key=self._embedding_api_key,
                api_base=self._embedding_api_base,
            ):
                endpoint = self._embedding_api_base or "OpenAI cloud"
                raise RuntimeError(
                    f"Embedding endpoint unreachable ({endpoint}) for model "
                    f"{self._embedding_model}. Check that the server is running "
                    "and the api_base is correct, or use mode='auto' for fallback."
                )

            embedding = self._get_embedding_backend()
            await embedding.fit_async(items)
            self._active_backend = "embedding"
            self._fitted = True
            self._fitted_mode = mode

        elif mode == SearchMode.AUTO:
            # Try embedding, fallback to keyword. Probe the endpoint so we
            # skip the expensive embedding call when the server is down
            # rather than waiting for it to fail.
            if not await is_embedding_reachable(
                model=self._embedding_model,
                api_key=self._embedding_api_key,
                api_base=self._embedding_api_base,
            ):
                # No embedding available - use keyword
                await self._fallback_to_keyword(
                    f"Embedding unavailable (no reachable endpoint for {self._embedding_model})",
                    items=items,
                )
            else:
                try:
                    embedding = self._get_embedding_backend()
                    await embedding.fit_async(items)
                    self._active_backend = "embedding"
                    self._fitted = True
                    self._fitted_mode = mode
                except Exception as e:
                    # Embedding failed - fallback to keyword
                    await self._fallback_to_keyword(
                        f"Embedding indexing failed: {e}",
                        error=e,
                        items=items,
                    )

        elif mode == SearchMode.HYBRID:
            # Both keyword and embedding
            keyword = self._get_keyword_backend()
            await keyword.fit_async(items)

            # Hybrid already tolerates runtime failures (see except below),
            # so the cheap config-only check is sufficient here — no need
            # to spend a probe round-trip on every fit.
            if is_embedding_configured(
                model=self._embedding_model,
                api_key=self._embedding_api_key,
                api_base=self._embedding_api_base,
            ):
                try:
                    embedding = self._get_embedding_backend()
                    await embedding.fit_async(items)
                    self._active_backend = "hybrid"
                except Exception as e:
                    logger.warning(f"Hybrid embedding indexing failed: {e}")
                    self._emit_fallback_event(
                        f"Hybrid mode embedding failed: {e}",
                        error=e,
                    )
                    self._active_backend = "keyword"
            else:
                self._emit_fallback_event(
                    f"Hybrid mode: embedding not configured for {self._embedding_model}"
                )
                self._active_backend = "keyword"

            self._fitted = True
            self._fitted_mode = mode

    async def search_async(
        self,
        query: str,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Search for items matching the query.

        Uses the appropriate backend(s) based on mode and fallback state.

        Args:
            query: Search query text
            top_k: Maximum number of results to return

        Returns:
            List of (item_id, similarity_score) tuples, sorted by
            relevance (highest first). Returns an empty list if the
            searcher has not been fitted.
        """
        if not self._fitted:
            return []

        # If we've already fallen back, use keyword search
        if self._using_fallback:
            return await self._get_keyword_backend().search_async(query, top_k)

        mode = self._config.get_mode_enum()

        # Check for mode mismatch between fit and search
        if self._fitted_mode is not None and self._fitted_mode != mode:
            logger.warning(
                f"Search mode changed from {self._fitted_mode.value} to {mode.value} "
                "since last fit. Falling back to keyword search. Call fit_async() to reindex."
            )
            if self._keyword_backend is not None and not self._keyword_backend.needs_refit():
                return await self._keyword_backend.search_async(query, top_k)
            await self._fallback_to_keyword(
                f"Mode changed from {self._fitted_mode.value} to {mode.value}"
            )
            return await self._get_keyword_backend().search_async(query, top_k)

        if mode == SearchMode.KEYWORD:
            return await self._get_keyword_backend().search_async(query, top_k)

        elif mode == SearchMode.EMBEDDING:
            # Verify embedding backend is actually fitted - strict mode, no fallback
            embedding_backend = self._get_embedding_backend()
            if embedding_backend.needs_refit():
                raise RuntimeError(
                    "Embedding backend unavailable or needs refit. "
                    "Call fit_async() first or use mode='auto' for fallback."
                )
            return await embedding_backend.search_async(query, top_k)

        elif mode == SearchMode.AUTO:
            # Try embedding, fallback to keyword on error
            embedding_backend = self._get_embedding_backend()
            # Defensively check if embedding backend is fitted
            if embedding_backend.needs_refit():
                logger.warning(
                    "Embedding backend needs refit in AUTO mode. Falling back to keyword."
                )
                await self._fallback_to_keyword("Embedding backend not properly fitted")
                return await self._get_keyword_backend().search_async(query, top_k)
            try:
                return await embedding_backend.search_async(query, top_k)
            except Exception as e:
                # Fallback to keyword
                await self._fallback_to_keyword(f"Embedding search failed: {e}", error=e)
                return await self._get_keyword_backend().search_async(query, top_k)

        elif mode == SearchMode.HYBRID:
            return await self._search_hybrid(query, top_k)

        return []

    async def _search_hybrid(
        self,
        query: str,
        top_k: int,
    ) -> list[tuple[str, float]]:
        """Perform hybrid search combining keyword and embedding scores."""
        keyword_weight, embedding_weight = self._config.get_normalized_weights()

        # Get keyword results
        keyword_results = await self._get_keyword_backend().search_async(query, top_k * 2)
        keyword_scores = dict(keyword_results)

        # Try to get embedding results
        embedding_scores: dict[str, float] = {}
        if self._embedding_backend is not None and not self._using_fallback:
            try:
                embedding_results = await self._embedding_backend.search_async(query, top_k * 2)
                embedding_scores = dict(embedding_results)
            except Exception as e:
                logger.warning(f"Hybrid embedding search failed: {e}")
                self._emit_fallback_event(f"Hybrid search embedding failed: {e}", error=e)
                # Continue with keyword only for this search

        # Combine scores
        all_ids = set(keyword_scores.keys()) | set(embedding_scores.keys())
        combined: list[tuple[str, float]] = []

        for item_id in all_ids:
            keyword_score = keyword_scores.get(item_id, 0.0)
            emb_score = embedding_scores.get(item_id, 0.0)
            combined_score = (keyword_weight * keyword_score) + (embedding_weight * emb_score)
            combined.append((item_id, combined_score))

        # Sort by combined score descending
        combined.sort(key=lambda x: x[1], reverse=True)

        return combined[:top_k]

    def get_active_backend(self) -> str:
        """Get the name of the currently active backend.

        Returns:
            One of "keyword", "embedding", "hybrid", or "none" if not fitted.
        """
        return self._active_backend or "none"

    def is_using_fallback(self) -> bool:
        """Check if search is currently using keyword fallback.

        Returns:
            True if using keyword backend due to embedding failure.
        """
        return self._using_fallback

    def get_fallback_reason(self) -> str | None:
        """Get the reason for fallback, if any.

        Returns:
            Human-readable fallback reason, or None if not using fallback.
        """
        return self._fallback_reason

    def needs_refit(self) -> bool:
        """Check if the search index needs rebuilding.

        Returns:
            True if fit_async() should be called before search_async().
        """
        if not self._fitted:
            return True

        mode = self._config.get_mode_enum()

        if mode == SearchMode.KEYWORD or self._using_fallback:
            return self._get_keyword_backend().needs_refit()

        if mode == SearchMode.EMBEDDING:
            return self._get_embedding_backend().needs_refit()

        if mode == SearchMode.HYBRID:
            keyword_needs = self._get_keyword_backend().needs_refit()
            embedding_needs = (
                self._embedding_backend.needs_refit() if self._embedding_backend else False
            )
            return keyword_needs or embedding_needs

        if mode == SearchMode.AUTO:
            # _using_fallback case already handled above in the KEYWORD branch
            return self._get_embedding_backend().needs_refit()

        return True

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about the search backends.

        Returns:
            Dict with unified statistics including active backend info.
        """
        stats: dict[str, Any] = {
            "mode": self._config.mode,
            "fitted": self._fitted,
            "fitted_mode": self._fitted_mode.value if self._fitted_mode else None,
            "active_backend": self._active_backend,
            "using_fallback": self._using_fallback,
            "fallback_reason": self._fallback_reason,
            "item_count": len(self._items),
        }

        if self._keyword_backend:
            stats["keyword"] = self._keyword_backend.get_stats()

        if self._embedding_backend:
            stats["embedding"] = self._embedding_backend.get_stats()

        return stats

    def clear(self) -> None:
        """Clear all search indexes and reset state."""
        if self._keyword_backend:
            self._keyword_backend.clear()
        if self._embedding_backend:
            self._embedding_backend.clear()

        self._items = []
        self._fitted = False
        self._fitted_mode = None
        self._using_fallback = False
        self._fallback_reason = None
        self._active_backend = None

    def mark_update(self) -> None:
        """Mark that an item update occurred.

        Call this after adding/updating/removing items to track
        when a refit is needed.
        """
        if self._keyword_backend and hasattr(self._keyword_backend, "mark_update"):
            self._keyword_backend.mark_update()
        # Embedding backend tracks updates through fitted state
