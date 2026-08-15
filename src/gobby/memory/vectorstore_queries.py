"""Query and point operations for :mod:`gobby.memory.vectorstore`."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from qdrant_client.models import Filter, FilterSelector, PointIdsList, PointStruct, QueryRequest

from gobby.memory.vectorstore_client import (
    QDRANT_CLIENT_TIMEOUT_SECONDS,
    QdrantClientLike,
)
from gobby.memory.vectorstore_filters import payload_filter

if TYPE_CHECKING:
    from gobby.memory.vectorstore import VectorStore

STORED_VECTOR_BATCH_SIZE = 50


class VectorStoreQueries:
    """Own vector point writes, searches, counts, and scrolling."""

    def __init__(self, store: VectorStore) -> None:
        self._store = store

    async def upsert(
        self,
        memory_id: str,
        embedding: list[float],
        payload: dict[str, Any] | None = None,
        collection_name: str | None = None,
    ) -> None:
        """Insert or update a single point."""
        store = self._store
        client = await store._ensure_initialized()
        point = PointStruct(id=memory_id, vector=embedding, payload=payload or {})
        try:
            await store._call_client(
                client,
                "upsert",
                collection_name=collection_name or store._collection_name,
                points=[point],
            )
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise

    async def search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | Filter | None = None,
        collection_name: str | None = None,
        timeout: float | None = None,
    ) -> list[tuple[str, float]]:
        """Search for similar vectors."""
        store = self._store
        client = await store._ensure_initialized()
        try:
            results = await store._call_client(
                client,
                "query_points",
                collection_name=collection_name or store._collection_name,
                query=query_embedding,
                query_filter=payload_filter(filters),
                limit=limit,
                timeout=timeout,
            )
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise
        return [(str(point.id), point.score) for point in results.points]

    async def search_with_payload(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str] | None = None,
        collection_name: str | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """Search for similar vectors and return their payloads."""
        store = self._store
        client = await store._ensure_initialized()
        query_filter = payload_filter(filters) if filters else None
        try:
            results = await store._call_client(
                client,
                "query_points",
                collection_name=collection_name or store._collection_name,
                query=query_embedding,
                query_filter=query_filter,
                limit=limit,
            )
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise
        return [(str(point.id), point.score, point.payload or {}) for point in results.points]

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
        if not ids:
            return {}
        store = self._store
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (
            timeout if timeout is not None else float(QDRANT_CLIENT_TIMEOUT_SECONDS)
        )

        def remaining() -> float:
            budget = deadline - loop.time()
            if budget <= 0:
                raise TimeoutError("Stored-vector search deadline expired")
            return budget

        client = await store._ensure_initialized(timeout=remaining())
        resolved_collection = collection_name or store._collection_name
        try:
            records = await store._call_client(
                client,
                "retrieve",
                collection_name=resolved_collection,
                ids=ids,
                with_payload=False,
                with_vectors=True,
                timeout=remaining(),
            )
            stored = [(str(record.id), record.vector) for record in records if record.vector]
            result: dict[str, list[tuple[str, float]]] = {}
            for start in range(0, len(stored), STORED_VECTOR_BATCH_SIZE):
                batch = stored[start : start + STORED_VECTOR_BATCH_SIZE]
                requests = [
                    QueryRequest(
                        query=vector,
                        filter=query_filter,
                        limit=limit,
                        with_payload=False,
                        with_vector=False,
                    )
                    for _memory_id, vector in batch
                ]
                responses = await store._call_client(
                    client,
                    "query_batch_points",
                    collection_name=resolved_collection,
                    requests=requests,
                    timeout=remaining(),
                )
                for (memory_id, _vector), response in zip(batch, responses, strict=True):
                    result[memory_id] = [
                        (str(point.id), float(point.score)) for point in response.points
                    ]
            return result
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise

    async def set_payload(
        self,
        memory_id: str,
        payload: dict[str, Any],
        collection_name: str | None = None,
    ) -> None:
        """Update payload fields on a point without re-embedding."""
        store = self._store
        client = await store._ensure_initialized()
        try:
            await store._call_client(
                client,
                "set_payload",
                collection_name=collection_name or store._collection_name,
                payload=payload,
                points=[memory_id],
            )
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise

    async def delete(
        self,
        memory_id: str | None = None,
        filters: dict[str, str] | None = None,
        collection_name: str | None = None,
    ) -> None:
        """Delete a point by memory ID or filter."""
        store = self._store
        client = await store._ensure_initialized()
        selector: PointIdsList | FilterSelector
        if memory_id:
            selector = PointIdsList(points=[memory_id])
        elif filters:
            query_filter = payload_filter(filters)
            if query_filter is None:
                raise ValueError("Must provide either memory_id or filters to delete")
            selector = FilterSelector(filter=query_filter)
        else:
            raise ValueError("Must provide either memory_id or filters to delete")
        try:
            await store._call_client(
                client,
                "delete",
                collection_name=collection_name or store._collection_name,
                points_selector=selector,
            )
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise

    async def delete_many(
        self,
        memory_ids: list[str],
        collection_name: str | None = None,
    ) -> None:
        """Delete multiple points by memory ID in a single batch call."""
        if not memory_ids:
            return
        store = self._store
        client = await store._ensure_initialized()
        selector = PointIdsList(points=memory_ids)
        try:
            await store._call_client(
                client,
                "delete",
                collection_name=collection_name or store._collection_name,
                points_selector=selector,
            )
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise

    async def batch_upsert(
        self,
        items: list[tuple[str, list[float], dict[str, Any]]],
        collection_name: str | None = None,
        *,
        client: QdrantClientLike | None = None,
    ) -> None:
        """Insert or update multiple points at once."""
        if not items:
            return
        store = self._store
        if client is None:
            client = await store._ensure_initialized()
        points = [
            PointStruct(id=memory_id, vector=embedding, payload=payload)
            for memory_id, embedding, payload in items
        ]
        try:
            await store._call_client(
                client,
                "upsert",
                collection_name=collection_name or store._collection_name,
                points=points,
            )
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise

    async def count(self) -> int:
        """Return the number of points in the default collection."""
        store = self._store
        client = await store._ensure_initialized()
        try:
            result = await store._call_client(
                client,
                "count",
                collection_name=store._collection_name,
            )
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise
        count: int = result.count
        return count

    async def scroll_ids(
        self,
        batch_size: int = 1000,
        filters: dict[str, str] | None = None,
    ) -> list[str]:
        """Return point IDs, optionally filtered by payload."""
        store = self._store
        client = await store._ensure_initialized()
        all_ids: list[str] = []
        offset = None
        scroll_filter = payload_filter(filters) if filters else None
        while True:
            try:
                points, next_offset = await store._call_client(
                    client,
                    "scroll",
                    collection_name=store._collection_name,
                    limit=batch_size,
                    offset=offset,
                    scroll_filter=scroll_filter,
                    with_payload=False,
                    with_vectors=False,
                )
            except Exception as exc:
                store._raise_if_recoverable(exc)
                raise
            all_ids.extend(str(point.id) for point in points)
            if next_offset is None:
                break
            offset = next_offset
        return all_ids
