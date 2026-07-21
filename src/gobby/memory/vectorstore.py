"""Qdrant-based vector store for memory embeddings.

Wraps qdrant-client with async support via asyncio.to_thread().
Supports embedded mode (on-disk, zero Docker) or remote Qdrant server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from qdrant_client.http.models.models import (
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
)
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from gobby.memory.vectorstore_rebuild import RebuildCollectionPlan
from gobby.memory.vectorstore_status import VectorStoreStatus
from gobby.storage.memories_scope import MemoryScope, MemoryScopeKind

logger = logging.getLogger(__name__)

_UNINITIALIZED_MESSAGE = "VectorStore not initialized. Call initialize() first."
_INITIAL_RETRY_BACKOFF_SECONDS = 5.0
_MAX_RETRY_BACKOFF_SECONDS = 300.0
_QDRANT_CLIENT_CLOSE_ERRORS = (
    OSError,
    RuntimeError,
    ResponseHandlingException,
    UnexpectedResponse,
    httpx.TransportError,
)
StaleDeleteStrategy = Literal["precompute", "streaming"]


def _vector_size(vectors_cfg: Any) -> int | None:
    """Extract vector size from a Qdrant vectors config when available."""
    return vectors_cfg.size if isinstance(vectors_cfg, VectorParams) else None


class VectorStoreUnavailableError(RuntimeError):
    """Raised when Qdrant is temporarily unavailable and lazy retry is backing off."""

    def __init__(self, message: str = _UNINITIALIZED_MESSAGE) -> None:
        super().__init__(message)


class VectorStoreCollectionDimensionError(RuntimeError):
    """Raised when an existing collection has an unexpected vector dimension."""


def is_recoverable_vector_store_error(error: BaseException) -> bool:
    """Return True for transient VectorStore/Qdrant availability errors."""
    if isinstance(error, VectorStoreUnavailableError | ResponseHandlingException):
        return True
    if isinstance(error, UnexpectedResponse):
        return error.status_code is not None and 500 <= error.status_code < 600
    return isinstance(error, httpx.TransportError)


def memory_scope_filter(scope: MemoryScope) -> Filter | None:
    """Return a Qdrant filter for an explicit memory scope."""
    if scope.kind is MemoryScopeKind.ALL:
        return None
    if scope.kind is MemoryScopeKind.GLOBAL_ONLY:
        return Filter(
            must=[
                FieldCondition(key="is_global", match=MatchValue(value=True)),
            ]
        )
    project_condition = FieldCondition(
        key="project_id",
        match=MatchValue(value=scope.project_id),
    )
    if scope.kind is MemoryScopeKind.OWNER:
        return Filter(must=[project_condition])
    if scope.kind is MemoryScopeKind.PROJECT_ONLY:
        return Filter(
            must=[
                project_condition,
                FieldCondition(key="is_global", match=MatchValue(value=False)),
            ]
        )
    return Filter(
        should=[
            project_condition,
            FieldCondition(key="is_global", match=MatchValue(value=True)),
        ]
    )


class VectorStore:
    """Async wrapper around Qdrant for memory vector storage.

    Uses embedded mode (path) for local operation or remote mode (url) for
    external Qdrant servers. All blocking qdrant-client calls are wrapped
    in asyncio.to_thread() for async compatibility.

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
    ) -> None:
        self._path = path
        self._url = url
        self._api_key = api_key
        self._collection_name = collection_name
        self._embedding_dim = embedding_dim
        self._client: QdrantClient | None = None
        self._init_lock = asyncio.Lock()
        self._rebuild_lock = asyncio.Lock()
        self._collection_lifecycle_lock = asyncio.Lock()
        self._retry_backoff_seconds = _INITIAL_RETRY_BACKOFF_SECONDS
        self._next_retry_at = 0.0
        self._status = VectorStoreStatus(self._collection_name, self._embedding_dim)

    @property
    def collection_name(self) -> str:
        """Return the default collection name configured for this store."""
        return self._collection_name

    def status_snapshot(self) -> dict[str, Any]:
        """Return structured collection lifecycle and recovery state."""
        return self._status.snapshot()

    async def initialize(self) -> None:
        """Create the Qdrant client and ensure the collection exists."""
        async with self._init_lock:
            try:
                await self._initialize_locked()
            except Exception as exc:
                self._raise_if_recoverable(exc)
                await self.close()
                raise
            self._reset_retry()

    async def _initialize_locked(self) -> None:
        """Initialize the client while the caller holds _init_lock."""
        if self._client is None:
            if self._url:
                self._client = await asyncio.to_thread(
                    QdrantClient, url=self._url, api_key=self._api_key
                )
            else:
                self._client = await asyncio.to_thread(QdrantClient, path=self._path)

        client = self._client
        if client is None:
            raise RuntimeError(_UNINITIALIZED_MESSAGE)
        async with self._collection_lifecycle_lock:
            exists = await asyncio.to_thread(client.collection_exists, self._collection_name)
            if not exists:
                created = await self._create_collection(
                    client,
                    self._collection_name,
                    self._embedding_dim,
                )
                if created:
                    logger.info(
                        "Created Qdrant collection '%s' (dim=%s, distance=cosine)",
                        self._collection_name,
                        self._embedding_dim,
                    )
                self._status.mark_ready()
            else:
                # Check for dimension mismatch between config and existing collection.
                try:
                    existing_dim = await self._read_collection_dimension(
                        client,
                        self._collection_name,
                    )
                    if existing_dim is not None and existing_dim != self._embedding_dim:
                        self._status.mark_dimension_mismatch(existing_dim)
                    else:
                        self._status.mark_ready()
                except VectorStoreCollectionDimensionError:
                    raise
                except Exception as e:
                    self._raise_if_recoverable(e)
                    logger.warning(
                        "Could not verify collection dimensions for '%s': %s",
                        self._collection_name,
                        e,
                    )

    async def _ensure_initialized(self) -> QdrantClient:
        """Return an initialized client, lazily retrying after recoverable failures."""
        if self._client is not None:
            return self._client

        if time.monotonic() < self._next_retry_at:
            raise VectorStoreUnavailableError()

        async with self._init_lock:
            if self._client is not None:
                return self._client

            if time.monotonic() < self._next_retry_at:
                raise VectorStoreUnavailableError()

            try:
                await self._initialize_locked()
            except Exception as exc:
                self._raise_if_recoverable(exc)
                raise
            self._reset_retry()

        if self._client is None:
            raise VectorStoreUnavailableError()
        return self._client

    def _raise_if_recoverable(self, error: Exception) -> None:
        if not is_recoverable_vector_store_error(error):
            return
        self._mark_unavailable(error)
        raise VectorStoreUnavailableError() from error

    def _mark_unavailable(self, error: BaseException) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except _QDRANT_CLIENT_CLOSE_ERRORS as close_error:
                logger.warning(
                    "Failed to close Qdrant client during mark_unavailable: %s",
                    close_error,
                )
        delay = self._retry_backoff_seconds
        self._next_retry_at = time.monotonic() + delay
        self._retry_backoff_seconds = min(delay * 2, _MAX_RETRY_BACKOFF_SECONDS)
        logger.warning(
            "VectorStore unavailable; retrying initialization in %.0fs: %s",
            delay,
            error,
        )

    def _reset_retry(self) -> None:
        self._retry_backoff_seconds = _INITIAL_RETRY_BACKOFF_SECONDS
        self._next_retry_at = 0.0

    async def _read_collection_dimension(
        self,
        client: QdrantClient,
        collection_name: str,
    ) -> int | None:
        info = await asyncio.to_thread(client.get_collection, collection_name)
        return _vector_size(info.config.params.vectors)

    async def _collection_has_expected_dimension(
        self,
        client: QdrantClient,
        collection_name: str,
        dim: int,
    ) -> bool:
        try:
            return await self._read_collection_dimension(client, collection_name) == dim
        except Exception as exc:
            logger.warning(
                "Could not verify Qdrant collection '%s' after create conflict: %s",
                collection_name,
                exc,
            )
            return False

    async def _create_collection(
        self,
        client: QdrantClient,
        collection_name: str,
        dim: int,
    ) -> bool:
        try:
            await asyncio.to_thread(
                client.create_collection,
                collection_name=collection_name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            return True
        except UnexpectedResponse as exc:
            is_create_conflict = getattr(exc, "status_code", None) == 409
            if is_create_conflict and await self._collection_has_expected_dimension(
                client,
                collection_name,
                dim,
            ):
                logger.info(
                    "Qdrant collection '%s' already exists with expected dim=%s",
                    collection_name,
                    dim,
                )
                return False
            self._raise_if_recoverable(exc)
            raise
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise

    async def upsert(
        self,
        memory_id: str,
        embedding: list[float],
        payload: dict[str, Any] | None = None,
        collection_name: str | None = None,
    ) -> None:
        """Insert or update a single point."""
        client = await self._ensure_initialized()
        point = PointStruct(
            id=memory_id,
            vector=embedding,
            payload=payload or {},
        )
        try:
            await asyncio.to_thread(
                client.upsert,
                collection_name=collection_name or self._collection_name,
                points=[point],
            )
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise

    async def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | Filter | None = None,
        collection_name: str | None = None,
    ) -> list[tuple[str, float]]:
        """Search for similar vectors.

        Args:
            query_embedding: Query vector.
            limit: Maximum number of results.
            filters: Optional field filters (e.g. {"project_id": "proj-A"}).
            collection_name: Optional collection name override.

        Returns:
            List of (memory_id, score) tuples sorted by relevance (desc).
        """
        client = await self._ensure_initialized()

        query_filter = None
        if filters is not None:
            if isinstance(filters, Filter):
                query_filter = filters
            else:
                conditions = [
                    FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items()
                ]
                query_filter = Filter(must=conditions)

        try:
            results = await asyncio.to_thread(
                client.query_points,
                collection_name=collection_name or self._collection_name,
                query=query_embedding,
                query_filter=query_filter,
                limit=limit,
            )
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise

        return [(str(point.id), point.score) for point in results.points]

    async def search_with_payload(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | None = None,
        collection_name: str | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """Search for similar vectors, returning payloads.

        Args:
            query_embedding: Query vector.
            limit: Maximum number of results.
            filters: Optional field filters (e.g. {"project_id": "proj-A"}).
            collection_name: Optional collection name override.

        Returns:
            List of (memory_id, score, payload) tuples sorted by relevance (desc).
        """
        client = await self._ensure_initialized()

        query_filter = None
        if filters:
            conditions = [
                FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items()
            ]
            query_filter = Filter(must=conditions)

        try:
            results = await asyncio.to_thread(
                client.query_points,
                collection_name=collection_name or self._collection_name,
                query=query_embedding,
                query_filter=query_filter,
                limit=limit,
            )
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise

        return [(str(point.id), point.score, point.payload or {}) for point in results.points]

    async def set_payload(
        self,
        memory_id: str,
        payload: dict[str, Any],
        collection_name: str | None = None,
    ) -> None:
        """Update payload fields on a point without re-embedding.

        Args:
            memory_id: The point ID to update.
            payload: Payload fields to set/overwrite.
            collection_name: Optional collection name override.
        """
        client = await self._ensure_initialized()
        try:
            await asyncio.to_thread(
                client.set_payload,
                collection_name=collection_name or self._collection_name,
                payload=payload,
                points=[memory_id],
            )
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise

    async def delete(
        self,
        memory_id: str | None = None,
        filters: dict[str, str] | None = None,
        collection_name: str | None = None,
    ) -> None:
        """Delete a point by memory ID or filter."""
        client = await self._ensure_initialized()

        selector: PointIdsList | FilterSelector
        if memory_id:
            selector = PointIdsList(points=[memory_id])
        elif filters:
            conditions = [
                FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items()
            ]
            selector = FilterSelector(filter=Filter(must=conditions))
        else:
            raise ValueError("Must provide either memory_id or filters to delete")

        try:
            await asyncio.to_thread(
                client.delete,
                collection_name=collection_name or self._collection_name,
                points_selector=selector,
            )
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise

    async def delete_many(
        self,
        memory_ids: list[str],
        collection_name: str | None = None,
    ) -> None:
        """Delete multiple points by memory ID in a single batch call."""
        if not memory_ids:
            return
        client = await self._ensure_initialized()
        selector = PointIdsList(points=memory_ids)
        try:
            await asyncio.to_thread(
                client.delete,
                collection_name=collection_name or self._collection_name,
                points_selector=selector,
            )
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise

    async def batch_upsert(
        self,
        items: list[tuple[str, list[float], dict[str, Any]]],
        collection_name: str | None = None,
    ) -> None:
        """Insert or update multiple points at once.

        Args:
            items: List of (memory_id, embedding, payload) tuples.
            collection_name: Optional collection name override.
        """
        if not items:
            return
        client = await self._ensure_initialized()
        points = [
            PointStruct(id=memory_id, vector=embedding, payload=payload)
            for memory_id, embedding, payload in items
        ]
        try:
            await asyncio.to_thread(
                client.upsert,
                collection_name=collection_name or self._collection_name,
                points=points,
            )
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise

    async def get_collection_dimension(self, collection_name: str | None = None) -> int | None:
        """Return the vector dimension for a collection when readable."""
        client = await self._ensure_initialized()
        resolved_name = collection_name or self._collection_name
        try:
            return await self._read_collection_dimension(client, resolved_name)
        except Exception as exc:
            self._raise_if_recoverable(exc)
            logger.warning(
                "Failed to read Qdrant collection dimension for '%s': %s",
                resolved_name,
                exc,
            )
            return None

    async def _prepare_collection_for_rebuild(
        self,
        client: QdrantClient,
        *,
        recreate_on_mismatch: bool = True,
    ) -> RebuildCollectionPlan:
        """Choose a rebuild target without modifying the active collection."""
        try:
            aliases_response = await asyncio.to_thread(client.get_aliases)
            alias_targets = {
                alias.alias_name: alias.collection_name for alias in aliases_response.aliases
            }
            active_alias_target = alias_targets.get(self._collection_name)
            exists = await asyncio.to_thread(client.collection_exists, self._collection_name)
            if not exists:
                created = await self._create_collection(
                    client,
                    self._collection_name,
                    self._embedding_dim,
                )
                if created:
                    logger.info(
                        "Created Qdrant collection '%s' for rebuild (dim=%s)",
                        self._collection_name,
                        self._embedding_dim,
                    )
                return RebuildCollectionPlan(
                    target_name=self._collection_name,
                    target_is_empty=created,
                )

            existing_dim = await self._read_collection_dimension(client, self._collection_name)
            if existing_dim is not None and existing_dim != self._embedding_dim:
                if not recreate_on_mismatch:
                    raise VectorStoreCollectionDimensionError(
                        f"Qdrant collection '{self._collection_name}' dimension mismatch "
                        f"(expected_dim={self._embedding_dim}, observed_dim={existing_dim})"
                    )
                target_name = f"{self._collection_name}@rebuild-{time.time_ns()}"
                created = await self._create_collection(
                    client,
                    target_name,
                    self._embedding_dim,
                )
                if not created:
                    raise RuntimeError(f"Could not create rebuild collection '{target_name}'")
                logger.info(
                    "Created temporary Qdrant collection '%s' for dimension change %s->%s",
                    target_name,
                    existing_dim,
                    self._embedding_dim,
                )
                return RebuildCollectionPlan(
                    target_name=target_name,
                    target_is_empty=True,
                    active_target=active_alias_target or self._collection_name,
                    active_is_alias=active_alias_target is not None,
                )
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise
        return RebuildCollectionPlan(
            target_name=self._collection_name,
            target_is_empty=False,
        )

    async def _activate_rebuild_collection(
        self,
        client: QdrantClient,
        plan: RebuildCollectionPlan,
    ) -> None:
        """Activate a fully populated rebuild target."""
        operations: list[DeleteAliasOperation | CreateAliasOperation] = []
        if plan.active_is_alias:
            operations.append(
                DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=self._collection_name))
            )
        else:
            await asyncio.to_thread(
                client.delete_collection,
                collection_name=self._collection_name,
            )
        operations.append(
            CreateAliasOperation(
                create_alias=CreateAlias(
                    collection_name=plan.target_name,
                    alias_name=self._collection_name,
                )
            )
        )
        await asyncio.to_thread(
            client.update_collection_aliases,
            change_aliases_operations=operations,
        )

    async def _delete_collection_best_effort(
        self,
        client: QdrantClient,
        collection_name: str,
    ) -> None:
        try:
            await asyncio.to_thread(
                client.delete_collection,
                collection_name=collection_name,
            )
        except Exception as exc:
            logger.warning("Could not delete obsolete collection '%s': %s", collection_name, exc)

    async def ensure_collection(
        self,
        collection_name: str,
        embedding_dim: int | None = None,
        *,
        recreate_on_mismatch: bool = False,
    ) -> None:
        """Ensure a named collection exists, creating it if needed.

        Args:
            collection_name: Collection to ensure
            embedding_dim: Vector dimension (defaults to instance's _embedding_dim)
            recreate_on_mismatch: Whether to recreate existing collections with
                a different dimension.
        """
        client = await self._ensure_initialized()
        dim = embedding_dim or self._embedding_dim
        async with self._collection_lifecycle_lock:
            try:
                exists = await asyncio.to_thread(client.collection_exists, collection_name)
            except Exception as exc:
                self._raise_if_recoverable(exc)
                raise
            if not exists:
                created = await self._create_collection(client, collection_name, dim)
                if created:
                    logger.info(
                        "Created Qdrant collection '%s' (dim=%s)",
                        collection_name,
                        dim,
                    )
            else:
                try:
                    existing_dim = await self._read_collection_dimension(client, collection_name)
                    if existing_dim is not None and existing_dim != dim:
                        if not recreate_on_mismatch:
                            logger.warning(
                                "Qdrant collection '%s' dimension mismatch "
                                "(expected_dim=%s, observed_dim=%s); leaving unchanged",
                                collection_name,
                                dim,
                                existing_dim,
                            )
                            raise VectorStoreCollectionDimensionError(
                                f"Qdrant collection '{collection_name}' dimension mismatch "
                                f"(expected_dim={dim}, observed_dim={existing_dim}). "
                                "Rebuild or migrate the collection to the configured embedding "
                                "dimension. Local/default nomic embeddings are usually 768 "
                                "dimensions; when intentionally using OpenAI "
                                "text-embedding-3-small, configure or pass embedding_dim=1536."
                            )
                        await asyncio.to_thread(
                            client.delete_collection,
                            collection_name=collection_name,
                        )
                        created = await self._create_collection(client, collection_name, dim)
                        if created:
                            logger.info(
                                "Recreated Qdrant collection '%s' (dim changed %s->%s)",
                                collection_name,
                                existing_dim,
                                dim,
                            )
                except (VectorStoreCollectionDimensionError, VectorStoreUnavailableError):
                    raise
                except Exception as e:
                    self._raise_if_recoverable(e)
                    logger.warning("Could not verify collection '%s': %s", collection_name, e)

    async def delete_collection(self, collection_name: str) -> None:
        """Delete a collection by name."""
        client = await self._ensure_initialized()
        async with self._collection_lifecycle_lock:
            try:
                await asyncio.to_thread(
                    client.delete_collection,
                    collection_name=collection_name,
                )
            except Exception as exc:
                self._raise_if_recoverable(exc)
                raise

    async def create_alias(self, collection_name: str, alias_name: str) -> None:
        """Create or repoint an alias to a physical collection.

        If the alias already exists, it is repointed to the new collection.
        """
        client = await self._ensure_initialized()
        async with self._collection_lifecycle_lock:
            try:
                await asyncio.to_thread(
                    client.update_collection_aliases,
                    change_aliases_operations=[
                        CreateAliasOperation(
                            create_alias=CreateAlias(
                                collection_name=collection_name,
                                alias_name=alias_name,
                            )
                        )
                    ],
                )
            except Exception as exc:
                self._raise_if_recoverable(exc)
                raise

    async def delete_alias(self, alias_name: str) -> None:
        """Delete an alias. Does not delete the underlying collection."""
        client = await self._ensure_initialized()
        async with self._collection_lifecycle_lock:
            try:
                await asyncio.to_thread(
                    client.update_collection_aliases,
                    change_aliases_operations=[
                        DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=alias_name))
                    ],
                )
            except Exception as exc:
                self._raise_if_recoverable(exc)
                raise

    async def get_aliases(self) -> dict[str, str]:
        """Return a mapping of alias_name -> collection_name for all aliases."""
        client = await self._ensure_initialized()
        try:
            response = await asyncio.to_thread(client.get_aliases)
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise
        result: dict[str, str] = {}
        for alias in response.aliases:
            result[alias.alias_name] = alias.collection_name
        return result

    async def count(self) -> int:
        """Return the number of points in the collection."""
        client = await self._ensure_initialized()
        try:
            result = await asyncio.to_thread(client.count, collection_name=self._collection_name)
        except Exception as exc:
            self._raise_if_recoverable(exc)
            raise
        count: int = result.count
        return count

    def count_sync(self) -> int:
        """Return the number of points in the collection (synchronous).

        Safe to call from sync code running inside an async event loop.
        """
        client = self._client
        if client is None:
            raise VectorStoreUnavailableError("Vector store is not initialized")
        try:
            result = client.count(collection_name=self._collection_name)
        except Exception as exc:
            if is_recoverable_vector_store_error(exc):
                self._mark_unavailable(exc)
                raise VectorStoreUnavailableError("Vector store count is unavailable") from exc
            raise
        return result.count

    async def rebuild(
        self,
        memories: list[dict[str, Any]],
        embed_fn: Callable[[str], Awaitable[list[float]]],
        *,
        recreate_on_mismatch: bool = True,
        stale_delete_strategy: StaleDeleteStrategy = "precompute",
    ) -> None:
        """Rebuild the collection from a list of memories.

        Re-embeds the provided memory list and removes stale existing points.
        If stale deletion fails partway through, some stale points may remain;
        retrying rebuild is safe.

        Args:
            memories: List of dicts with at least 'id' and 'content' keys.
                      Other keys are stored as payload.
            embed_fn: Async function that takes content text and returns embedding.
        """
        async with self._rebuild_lock:
            client = await self._ensure_initialized()

            async with self._collection_lifecycle_lock:
                plan = await self._prepare_collection_for_rebuild(
                    client,
                    recreate_on_mismatch=recreate_on_mismatch,
                )

                activation_started = False
                try:
                    batch_size = 500
                    total = 0
                    if stale_delete_strategy not in ("precompute", "streaming"):
                        raise ValueError(
                            "stale_delete_strategy must be 'precompute' or 'streaming'"
                        )
                    incoming_ids: set[str] = (
                        {str(mem["id"]) for mem in memories}
                        if stale_delete_strategy == "precompute"
                        else set()
                    )
                    batch: list[tuple[str, list[float], dict[str, Any]]] = []
                    for mem in memories:
                        memory_id = str(mem["id"])
                        if stale_delete_strategy == "streaming":
                            incoming_ids.add(memory_id)
                        content = mem["content"]
                        embedding = await embed_fn(content)
                        payload = {k: v for k, v in mem.items() if k not in ("id",)}
                        batch.append((memory_id, embedding, payload))

                        if len(batch) >= batch_size:
                            if plan.target_name == self._collection_name:
                                await self.batch_upsert(batch)
                            else:
                                await self.batch_upsert(batch, collection_name=plan.target_name)
                            total += len(batch)
                            logger.info("Rebuild progress: %s/%s vectors", total, len(memories))
                            batch = []

                    if batch:
                        if plan.target_name == self._collection_name:
                            await self.batch_upsert(batch)
                        else:
                            await self.batch_upsert(batch, collection_name=plan.target_name)
                        total += len(batch)

                    if not plan.target_is_empty:
                        await self._delete_stale_ids(client, incoming_ids, batch_size=batch_size)

                    if plan.requires_swap:
                        activation_started = True
                        await self._activate_rebuild_collection(client, plan)
                except BaseException:
                    if plan.requires_swap and not activation_started:
                        await self._delete_collection_best_effort(client, plan.target_name)
                    raise

                if plan.active_is_alias and plan.active_target is not None:
                    await self._delete_collection_best_effort(client, plan.active_target)

                self._status.mark_rebuild_complete()
                logger.info("Rebuilt %s vectors in '%s'", total, self._collection_name)

    async def _delete_stale_ids(
        self,
        client: QdrantClient,
        incoming_ids: set[str],
        *,
        batch_size: int,
    ) -> None:
        offset = None
        # Precomputing stale IDs keeps Qdrant scroll pagination stable while
        # deleting. If collections grow too large, prefer a batched/streaming
        # deletion strategy that avoids holding every stale ID in memory.
        stale_ids: list[str] = []
        while True:
            try:
                points, next_offset = await asyncio.to_thread(
                    client.scroll,
                    collection_name=self._collection_name,
                    limit=batch_size,
                    offset=offset,
                    with_payload=False,
                    with_vectors=False,
                )
            except Exception as exc:
                self._raise_if_recoverable(exc)
                raise
            for point in points:
                point_id = str(point.id)
                if point_id in incoming_ids:
                    continue
                stale_ids.append(point_id)
            if next_offset is None:
                break
            offset = next_offset
        for index in range(0, len(stale_ids), batch_size):
            await self.delete_many(stale_ids[index : index + batch_size])
        logger.info("Deleted %s stale points from '%s'", len(stale_ids), self._collection_name)

    async def scroll_ids(
        self,
        batch_size: int = 1000,
        filters: dict[str, str] | None = None,
    ) -> list[str]:
        """Return point IDs in the collection, optionally filtered by payload."""
        client = await self._ensure_initialized()
        all_ids: list[str] = []
        offset = None
        scroll_filter = None
        if filters:
            conditions = [
                FieldCondition(key=key, match=MatchValue(value=value))
                for key, value in filters.items()
            ]
            scroll_filter = Filter(must=conditions)

        while True:
            try:
                points, next_offset = await asyncio.to_thread(
                    client.scroll,
                    collection_name=self._collection_name,
                    limit=batch_size,
                    offset=offset,
                    scroll_filter=scroll_filter,
                    with_payload=False,
                    with_vectors=False,
                )
            except Exception as exc:
                self._raise_if_recoverable(exc)
                raise
            all_ids.extend(str(p.id) for p in points)
            if next_offset is None:
                break
            offset = next_offset

        return all_ids

    async def close(self) -> None:
        """Close the Qdrant client connection."""
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None
