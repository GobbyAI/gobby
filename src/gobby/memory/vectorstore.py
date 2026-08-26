"""Qdrant-based vector store for memory embeddings.

Wraps focused vector-store components behind the stable VectorStore facade.
Supports embedded mode (on-disk, zero Docker) or remote Qdrant server.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from qdrant_client import AsyncQdrantClient, QdrantClient
from qdrant_client.models import Filter

from gobby.memory.vectorstore_client import (
    QdrantClientLike,
    VectorStoreClient,
)
from gobby.memory.vectorstore_client import (
    VectorStoreCollectionDimensionError as VectorStoreCollectionDimensionError,
)
from gobby.memory.vectorstore_client import (
    VectorStoreUnavailableError as VectorStoreUnavailableError,
)
from gobby.memory.vectorstore_client import (
    is_recoverable_vector_store_error as is_recoverable_vector_store_error,
)
from gobby.memory.vectorstore_filters import memory_scope_filter as memory_scope_filter
from gobby.memory.vectorstore_maintenance import (
    StaleDeleteStrategy,
    VectorStoreMaintenance,
)
from gobby.memory.vectorstore_queries import VectorStoreQueries
from gobby.memory.vectorstore_rebuild import RebuildCollectionPlan
from gobby.memory.vectorstore_status import VectorStoreStatus
from gobby.storage.embedding_generation_state import EmbeddingGenerationState

_COLLECTION_SOURCE_KINDS = {
    "memories": "memory",
    "tool_embeddings": "tool",
    "gobby_github_issues": "github_issue",
}


class VectorStore:
    """Async wrapper around Qdrant for memory vector storage.

    Uses embedded mode (path) for local operation or remote mode (url) for
    external Qdrant servers. Remote calls use the async client directly;
    embedded calls are offloaded because QdrantLocal is synchronous.

    Args:
        path: Directory path for embedded Qdrant storage.
        url: URL for remote Qdrant server.
        api_key: API key for remote Qdrant server.
        collection_name: Name of the Qdrant collection.
        embedding_dim: Dimensionality of embedding vectors.
    """

    def __init__(
        self,
        path: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        collection_name: str = "memories",
        embedding_dim: int = 768,
        serving_guard: Callable[[], None] | None = None,
        generation_state: EmbeddingGenerationState | None = None,
        projection_targets_provider: Callable[[str, str], tuple[str, ...]] | None = None,
    ) -> None:
        self._path = path
        self._url = url
        self._api_key = api_key
        self._collection_name = collection_name
        self._embedding_dim = embedding_dim
        self._serving_guard = serving_guard
        self._generation_state = generation_state
        self._projection_targets_provider = projection_targets_provider
        self._client: QdrantClientLike | None = None
        self._retired_clients: list[QdrantClientLike] = []
        self._init_lock = asyncio.Lock()
        self._rebuild_lock = asyncio.Lock()
        self._collection_lifecycle_lock = asyncio.Lock()
        self._retry_backoff_seconds = 5.0
        self._next_retry_at = 0.0
        self._status = VectorStoreStatus(self._collection_name, self._embedding_dim)
        self._client_ops = VectorStoreClient(
            self,
            lambda: time.monotonic(),
            lambda **kwargs: QdrantClient(**kwargs),
            lambda **kwargs: AsyncQdrantClient(**kwargs),
        )
        self._queries = VectorStoreQueries(self)
        self._maintenance = VectorStoreMaintenance(self)

    @property
    def collection_name(self) -> str:
        """Return the default collection name configured for this store."""
        return self._client_ops.collection_name

    async def _projection_targets(self, collection_name: str | None) -> tuple[str, ...]:
        primary = collection_name or self._collection_name
        source_kind = _COLLECTION_SOURCE_KINDS.get(primary.split("@", 1)[0])
        if source_kind is None:
            return (primary,)
        if self._projection_targets_provider is not None:
            return self._projection_targets_provider(source_kind, primary)
        if self._generation_state is None:
            return (primary,)
        return await asyncio.to_thread(
            self._generation_state.projection_targets, source_kind, primary
        )

    def status_snapshot(self) -> dict[str, Any]:
        """Return structured collection lifecycle and recovery state."""
        return self._client_ops.status_snapshot()

    async def initialize(self) -> None:
        """Create the Qdrant client and ensure the collection exists."""
        await self._client_ops.initialize()

    async def _initialize_locked(self) -> None:
        await self._client_ops.initialize_locked()

    @property
    def is_remote(self) -> bool:
        return self._client_ops.is_remote

    async def _ensure_initialized(self, timeout: float | None = None) -> QdrantClientLike:
        return await self._client_ops.ensure_initialized(timeout)

    async def _call_client(
        self,
        client: QdrantClientLike,
        method_name: str,
        *args: Any,
        timeout: float | None = None,
        timeout_hint: bool = True,
        **kwargs: Any,
    ) -> Any:
        if self._serving_guard is not None:
            self._serving_guard()
        return await self._client_ops.call(
            client,
            method_name,
            *args,
            timeout=timeout,
            timeout_hint=timeout_hint,
            **kwargs,
        )

    def _raise_if_recoverable(self, error: Exception) -> None:
        self._client_ops.raise_if_recoverable(error)

    def _mark_unavailable(self, error: BaseException) -> None:
        self._client_ops.mark_unavailable(error)

    def _reset_retry(self) -> None:
        self._client_ops.reset_retry()

    async def _read_collection_dimension(
        self,
        client: QdrantClientLike,
        collection_name: str,
    ) -> int | None:
        return await self._client_ops.read_collection_dimension(client, collection_name)

    async def _collection_has_expected_dimension(
        self,
        client: QdrantClientLike,
        collection_name: str,
        dim: int,
    ) -> bool:
        return await self._client_ops.collection_has_expected_dimension(
            client,
            collection_name,
            dim,
        )

    async def _create_collection(
        self,
        client: QdrantClientLike,
        collection_name: str,
        dim: int,
    ) -> bool:
        return await self._client_ops.create_collection(client, collection_name, dim)

    async def upsert(
        self,
        memory_id: str,
        embedding: list[float],
        payload: dict[str, Any] | None = None,
        collection_name: str | None = None,
    ) -> None:
        """Insert or update a single point."""
        for target in await self._projection_targets(collection_name):
            await self._queries.upsert(memory_id, embedding, payload, target)

    async def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | Filter | None = None,
        collection_name: str | None = None,
        timeout: float | None = None,
    ) -> list[tuple[str, float]]:
        """Search for similar vectors.

        Returns memory ID and score tuples sorted by descending relevance.
        """
        return await self._queries.search(
            query_embedding,
            limit,
            filters,
            collection_name,
            timeout=timeout,
        )

    async def score_ids(
        self,
        query_embedding: list[float],
        ids: list[str],
        collection_name: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, float]:
        """Return the cosine of each of ``ids`` against ``query_embedding``.

        Ids with no stored vector are absent from the result rather than zero.
        """
        return await self._queries.score_ids(
            query_embedding,
            ids,
            collection_name,
            timeout=timeout,
        )

    async def search_with_payload(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | None = None,
        collection_name: str | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """Search for similar vectors and return their payloads."""
        return await self._queries.search_with_payload(
            query_embedding,
            limit,
            filters,
            collection_name,
        )

    async def get_vectors(
        self,
        ids: list[str],
        *,
        collection_name: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, list[float]]:
        """Return stored vectors for ``ids``; ids without a vector are omitted."""
        return await self._queries.get_vectors(
            ids,
            collection_name=collection_name,
            timeout=timeout,
        )

    async def search_by_stored_vectors(
        self,
        ids: list[str],
        *,
        limit: int,
        query_filter: Filter | None = None,
        collection_name: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, list[tuple[str, float]]]:
        """Batch-search using vectors already stored for IDs."""
        return await self._queries.search_by_stored_vectors(
            ids,
            limit=limit,
            query_filter=query_filter,
            collection_name=collection_name,
            timeout=timeout,
        )

    async def set_payload(
        self,
        memory_id: str,
        payload: dict[str, Any],
        collection_name: str | None = None,
    ) -> None:
        """Update payload fields on a point without re-embedding."""
        for target in await self._projection_targets(collection_name):
            await self._queries.set_payload(memory_id, payload, target)

    async def delete(
        self,
        memory_id: str | None = None,
        filters: dict[str, str] | None = None,
        collection_name: str | None = None,
    ) -> None:
        """Delete a point by memory ID or filter."""
        for target in await self._projection_targets(collection_name):
            await self._queries.delete(memory_id, filters, target)

    async def delete_many(
        self,
        memory_ids: list[str],
        collection_name: str | None = None,
    ) -> None:
        """Delete multiple points by memory ID in a single batch call."""
        for target in await self._projection_targets(collection_name):
            await self._queries.delete_many(memory_ids, target)

    async def batch_upsert(
        self,
        items: list[tuple[str, list[float], dict[str, Any]]],
        collection_name: str | None = None,
    ) -> None:
        """Insert or update multiple points at once."""
        for target in await self._projection_targets(collection_name):
            await self._queries.batch_upsert(items, target)

    async def get_collection_dimension(self, collection_name: str | None = None) -> int | None:
        """Return the vector dimension for a collection when readable."""
        return await self._client_ops.get_collection_dimension(collection_name)

    async def _prepare_collection_for_rebuild(
        self,
        client: QdrantClientLike,
        *,
        recreate_on_mismatch: bool = True,
    ) -> RebuildCollectionPlan:
        return await self._maintenance.prepare_collection_for_rebuild(
            client,
            recreate_on_mismatch=recreate_on_mismatch,
        )

    async def _activate_rebuild_collection(
        self,
        client: QdrantClientLike,
        plan: RebuildCollectionPlan,
    ) -> None:
        await self._maintenance.activate_rebuild_collection(client, plan)

    async def _delete_collection_best_effort(
        self,
        client: QdrantClientLike,
        collection_name: str,
    ) -> None:
        await self._maintenance.delete_collection_best_effort(client, collection_name)

    async def ensure_collection(
        self,
        collection_name: str,
        embedding_dim: int | None = None,
        *,
        recreate_on_mismatch: bool = False,
    ) -> None:
        """Ensure a named collection exists, creating it if needed."""
        await self._client_ops.ensure_collection(
            collection_name,
            embedding_dim,
            recreate_on_mismatch=recreate_on_mismatch,
        )

    async def delete_collection(self, collection_name: str) -> None:
        """Delete a collection by name."""
        await self._client_ops.delete_collection(collection_name)

    async def create_alias(self, collection_name: str, alias_name: str) -> None:
        """Create or repoint an alias to a physical collection."""
        await self._client_ops.create_alias(collection_name, alias_name)

    async def delete_alias(self, alias_name: str) -> None:
        """Delete an alias without deleting its underlying collection."""
        await self._client_ops.delete_alias(alias_name)

    async def get_aliases(self) -> dict[str, str]:
        """Return a mapping of alias names to physical collection names."""
        return await self._client_ops.get_aliases()

    async def list_collection_names(self) -> list[str]:
        """Return every physical collection name visible to this store."""
        client = await self._ensure_initialized()
        response = await self._call_client(client, "get_collections")
        return [str(item.name) for item in response.collections]

    async def count(self) -> int:
        """Return the number of points in the collection."""
        return await self._queries.count()

    async def rebuild(
        self,
        memories: list[dict[str, Any]],
        embed_fn: Callable[[str], Awaitable[list[float]]],
        *,
        recreate_on_mismatch: bool = True,
        stale_delete_strategy: StaleDeleteStrategy = "precompute",
    ) -> None:
        """Re-embed memories, remove stale points, and activate dimension changes."""
        await self._maintenance.rebuild(
            memories,
            embed_fn,
            recreate_on_mismatch=recreate_on_mismatch,
            stale_delete_strategy=stale_delete_strategy,
        )

    async def _delete_stale_ids(
        self,
        client: QdrantClientLike,
        incoming_ids: set[str],
        *,
        batch_size: int,
    ) -> None:
        await self._maintenance.delete_stale_ids(
            client,
            incoming_ids,
            batch_size=batch_size,
        )

    async def scroll_ids(
        self,
        batch_size: int = 1000,
        filters: dict[str, str] | None = None,
    ) -> list[str]:
        """Return point IDs in the collection, optionally filtered by payload."""
        return await self._queries.scroll_ids(batch_size, filters)

    async def close(self) -> None:
        """Close the Qdrant client connection."""
        await self._client_ops.close()
