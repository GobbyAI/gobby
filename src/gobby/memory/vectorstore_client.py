"""Client and collection lifecycle operations for :mod:`gobby.memory.vectorstore`."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from qdrant_client import AsyncQdrantClient, QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from qdrant_client.http.models.models import (
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
)
from qdrant_client.models import Distance, VectorParams

from gobby.storage.embedding_generation_state import EmbeddingGenerationLeaseLost

if TYPE_CHECKING:
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)

QDRANT_CLIENT_TIMEOUT_SECONDS = 5
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
_QDRANT_TIMEOUT_HINT_METHODS = frozenset({"query_batch_points", "retrieve"})

type QdrantClientLike = QdrantClient | AsyncQdrantClient


def _accepts_timeout_kwarg(method: Any) -> bool:
    """Only some qdrant client methods declare ``timeout``; the rest reject
    unknown kwargs (e.g. ``upsert`` raises ``Unknown arguments: ['timeout']``)."""
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    param = params.get("timeout")
    return param is not None and param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


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
    if isinstance(
        error,
        VectorStoreUnavailableError | ResponseHandlingException | EmbeddingGenerationLeaseLost,
    ):
        return True
    if isinstance(error, UnexpectedResponse):
        return error.status_code is not None and 500 <= error.status_code < 600
    return isinstance(error, httpx.TransportError)


class VectorStoreClient:
    """Own Qdrant client initialization and collection lifecycle behavior."""

    def __init__(
        self,
        store: VectorStore,
        monotonic: Callable[[], float],
        local_client_factory: Callable[..., QdrantClient],
        remote_client_factory: Callable[..., AsyncQdrantClient],
    ) -> None:
        self._store = store
        self._monotonic = monotonic
        self._local_client_factory = local_client_factory
        self._remote_client_factory = remote_client_factory

    @property
    def is_remote(self) -> bool:
        return bool(self._store._url)

    async def call(
        self,
        client: QdrantClientLike,
        method_name: str,
        *args: Any,
        timeout: float | None = None,
        timeout_hint: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Execute a client operation using native remote awaits or local offload."""
        method = getattr(client, method_name)
        budget = timeout if timeout is not None else float(QDRANT_CLIENT_TIMEOUT_SECONDS)
        if budget <= 0:
            raise TimeoutError(f"Qdrant {method_name} deadline expired")
        if not self.is_remote:
            async with asyncio.timeout(budget):
                return await asyncio.to_thread(method, *args, **kwargs)

        if timeout_hint and (
            method_name in _QDRANT_TIMEOUT_HINT_METHODS or _accepts_timeout_kwarg(method)
        ):
            kwargs["timeout"] = max(1, math.ceil(budget))
        async with asyncio.timeout(budget):
            result = method(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

    @property
    def collection_name(self) -> str:
        """Return the default collection name configured for this store."""
        return self._store._collection_name

    def status_snapshot(self) -> dict[str, Any]:
        """Return structured collection lifecycle and recovery state."""
        return self._store._status.snapshot()

    async def initialize(self) -> None:
        """Create the Qdrant client and ensure the collection exists."""
        async with self._store._init_lock:
            try:
                await self._store._initialize_locked()
            except Exception as exc:
                self._store._raise_if_recoverable(exc)
                await self._store.close()
                raise
            self._store._reset_retry()

    async def initialize_locked(self) -> None:
        """Initialize the client while the caller holds the initialization lock."""
        store = self._store
        if store._client is None:
            await self._close_clients(store._retired_clients)
            store._retired_clients.clear()
            if store._url:
                store._client = self._remote_client_factory(
                    url=store._url,
                    api_key=store._api_key,
                    timeout=QDRANT_CLIENT_TIMEOUT_SECONDS,
                )
            else:
                store._client = await asyncio.to_thread(
                    self._local_client_factory,
                    path=store._path,
                )

        client = store._client
        if client is None:
            raise RuntimeError(_UNINITIALIZED_MESSAGE)
        async with store._collection_lifecycle_lock:
            exists = await self.call(
                client,
                "collection_exists",
                store._collection_name,
                timeout_hint=False,
            )
            if not exists:
                created = await store._create_collection(
                    client,
                    store._collection_name,
                    store._embedding_dim,
                )
                if created:
                    logger.info(
                        "Created Qdrant collection '%s' (dim=%s, distance=cosine)",
                        store._collection_name,
                        store._embedding_dim,
                    )
                store._status.mark_ready()
                return

            try:
                existing_dim = await store._read_collection_dimension(
                    client,
                    store._collection_name,
                )
                if existing_dim is not None and existing_dim != store._embedding_dim:
                    store._status.mark_dimension_mismatch(existing_dim)
                else:
                    store._status.mark_ready()
            except VectorStoreCollectionDimensionError:
                raise
            except Exception as exc:
                store._raise_if_recoverable(exc)
                logger.warning(
                    "Could not verify collection dimensions for '%s': %s",
                    store._collection_name,
                    exc,
                )

    async def ensure_initialized(self, timeout: float | None = None) -> QdrantClientLike:
        """Return an initialized client, lazily retrying recoverable failures."""
        if timeout is not None:
            if timeout <= 0:
                raise TimeoutError("Qdrant initialization deadline expired")
            async with asyncio.timeout(timeout):
                return await self.ensure_initialized()

        store = self._store
        if store._client is not None:
            return store._client

        if self._monotonic() < store._next_retry_at:
            raise VectorStoreUnavailableError()

        async with store._init_lock:
            if store._client is not None:
                return store._client
            if self._monotonic() < store._next_retry_at:
                raise VectorStoreUnavailableError()
            try:
                await store._initialize_locked()
            except Exception as exc:
                store._raise_if_recoverable(exc)
                raise
            store._reset_retry()

        if store._client is None:
            raise VectorStoreUnavailableError()
        return store._client

    def raise_if_recoverable(self, error: Exception) -> None:
        if not is_recoverable_vector_store_error(error):
            return
        self._store._mark_unavailable(error)
        raise VectorStoreUnavailableError() from error

    def mark_unavailable(self, error: BaseException) -> None:
        store = self._store
        client = store._client
        store._client = None
        if client is not None:
            store._retired_clients.append(client)
        delay = store._retry_backoff_seconds
        store._next_retry_at = self._monotonic() + delay
        store._retry_backoff_seconds = min(delay * 2, _MAX_RETRY_BACKOFF_SECONDS)
        logger.warning(
            "VectorStore unavailable; retrying initialization in %.0fs: %s",
            delay,
            error,
        )

    def reset_retry(self) -> None:
        self._store._retry_backoff_seconds = _INITIAL_RETRY_BACKOFF_SECONDS
        self._store._next_retry_at = 0.0

    async def read_collection_dimension(
        self,
        client: QdrantClientLike,
        collection_name: str,
    ) -> int | None:
        info = await self.call(client, "get_collection", collection_name)
        return _vector_size(info.config.params.vectors)

    async def collection_has_expected_dimension(
        self,
        client: QdrantClientLike,
        collection_name: str,
        dim: int,
    ) -> bool:
        try:
            return await self._store._read_collection_dimension(client, collection_name) == dim
        except Exception as exc:
            logger.warning(
                "Could not verify Qdrant collection '%s' after create conflict: %s",
                collection_name,
                exc,
            )
            return False

    async def create_collection(
        self,
        client: QdrantClientLike,
        collection_name: str,
        dim: int,
    ) -> bool:
        try:
            await self.call(
                client,
                "create_collection",
                collection_name=collection_name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            return True
        except UnexpectedResponse as exc:
            is_create_conflict = getattr(exc, "status_code", None) == 409
            if is_create_conflict and await self._store._collection_has_expected_dimension(
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
            self._store._raise_if_recoverable(exc)
            raise
        except Exception as exc:
            self._store._raise_if_recoverable(exc)
            raise

    async def get_collection_dimension(self, collection_name: str | None = None) -> int | None:
        """Return the vector dimension for a collection when readable."""
        store = self._store
        client = await store._ensure_initialized()
        resolved_name = collection_name or store._collection_name
        try:
            return await store._read_collection_dimension(client, resolved_name)
        except Exception as exc:
            store._raise_if_recoverable(exc)
            logger.warning(
                "Failed to read Qdrant collection dimension for '%s': %s",
                resolved_name,
                exc,
            )
            return None

    async def ensure_collection(
        self,
        collection_name: str,
        embedding_dim: int | None = None,
        *,
        recreate_on_mismatch: bool = False,
    ) -> None:
        """Ensure a named collection exists, creating it if needed."""
        store = self._store
        client = await store._ensure_initialized()
        dim = embedding_dim or store._embedding_dim
        async with store._collection_lifecycle_lock:
            try:
                exists = await self.call(
                    client,
                    "collection_exists",
                    collection_name,
                    timeout_hint=False,
                )
            except Exception as exc:
                store._raise_if_recoverable(exc)
                raise
            if not exists:
                created = await store._create_collection(client, collection_name, dim)
                if created:
                    logger.info("Created Qdrant collection '%s' (dim=%s)", collection_name, dim)
                return

            try:
                existing_dim = await store._read_collection_dimension(client, collection_name)
                if existing_dim is None or existing_dim == dim:
                    return
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
                await self.call(
                    client,
                    "delete_collection",
                    collection_name=collection_name,
                )
                created = await store._create_collection(client, collection_name, dim)
                if created:
                    logger.info(
                        "Recreated Qdrant collection '%s' (dim changed %s->%s)",
                        collection_name,
                        existing_dim,
                        dim,
                    )
            except (VectorStoreCollectionDimensionError, VectorStoreUnavailableError):
                raise
            except Exception as exc:
                store._raise_if_recoverable(exc)
                logger.warning("Could not verify collection '%s': %s", collection_name, exc)

    async def delete_collection(self, collection_name: str) -> None:
        """Delete a collection by name."""
        store = self._store
        client = await store._ensure_initialized()
        async with store._collection_lifecycle_lock:
            try:
                await self.call(
                    client,
                    "delete_collection",
                    collection_name=collection_name,
                )
            except Exception as exc:
                store._raise_if_recoverable(exc)
                raise

    async def create_alias(self, collection_name: str, alias_name: str) -> None:
        """Create or repoint an alias to a physical collection."""
        store = self._store
        client = await store._ensure_initialized()
        async with store._collection_lifecycle_lock:
            try:
                await self.call(
                    client,
                    "update_collection_aliases",
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
                store._raise_if_recoverable(exc)
                raise

    async def delete_alias(self, alias_name: str) -> None:
        """Delete an alias without deleting its underlying collection."""
        store = self._store
        client = await store._ensure_initialized()
        async with store._collection_lifecycle_lock:
            try:
                await self.call(
                    client,
                    "update_collection_aliases",
                    change_aliases_operations=[
                        DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=alias_name))
                    ],
                )
            except Exception as exc:
                store._raise_if_recoverable(exc)
                raise

    async def get_aliases(self) -> dict[str, str]:
        """Return a mapping of alias names to physical collection names."""
        store = self._store
        client = await store._ensure_initialized()
        try:
            response = await self.call(client, "get_aliases")
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise
        return {alias.alias_name: alias.collection_name for alias in response.aliases}

    async def close(self) -> None:
        """Close the Qdrant client connection."""
        store = self._store
        clients = [*store._retired_clients]
        store._retired_clients.clear()
        if store._client is not None:
            clients.append(store._client)
            store._client = None
        await self._close_clients(clients)

    async def _close_clients(self, clients: list[QdrantClientLike]) -> None:
        """Close each distinct client, containing teardown failures."""
        seen: set[int] = set()
        for client in clients:
            identity = id(client)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                await self.call(client, "close", timeout_hint=False)
            except _QDRANT_CLIENT_CLOSE_ERRORS as close_error:
                logger.warning("Failed to close Qdrant client: %s", close_error)
