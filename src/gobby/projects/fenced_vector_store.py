"""Project-aware write fencing wrapper for the daemon's shared vector store."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any, Protocol, cast

from gobby.memory.vectorstore import VectorStore


class VectorWriteFence(Protocol):
    def writer(self, project_id: str) -> AbstractAsyncContextManager[None]: ...
    def global_writer(self) -> AbstractAsyncContextManager[None]: ...


@asynccontextmanager
async def _unfenced_write_context() -> Any:
    yield


def project_write_context(
    vector_store: object | None,
    project_id: str,
) -> AbstractAsyncContextManager[None]:
    """Use service-level project admission when the store is daemon-fenced."""
    factory = getattr(vector_store, "project_write_context", None)
    if callable(factory) and not inspect.iscoroutinefunction(factory):
        return cast(AbstractAsyncContextManager[None], factory(project_id))
    return _unfenced_write_context()


def global_write_context(vector_store: object | None) -> AbstractAsyncContextManager[None]:
    """Use service-level global admission when the store is daemon-fenced."""
    factory = getattr(vector_store, "global_write_context", None)
    if callable(factory) and not inspect.iscoroutinefunction(factory):
        return cast(AbstractAsyncContextManager[None], factory())
    return _unfenced_write_context()


class ProjectFencedVectorStore:
    """Delegate vector operations while fencing every point-producing write."""

    def __init__(self, inner: VectorStore, fence: VectorWriteFence) -> None:
        self._inner = inner
        self._fence = fence

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def upsert(
        self,
        memory_id: str,
        embedding: list[float],
        payload: dict[str, Any] | None = None,
        collection_name: str | None = None,
    ) -> None:
        async with self._payload_writer(payload):
            await self._inner.upsert(memory_id, embedding, payload, collection_name)

    async def batch_upsert(
        self,
        items: list[tuple[str, list[float], dict[str, Any]]],
        collection_name: str | None = None,
    ) -> None:
        if not items:
            return
        project_ids = {
            str(payload["project_id"])
            for _point_id, _embedding, payload in items
            if payload.get("project_id") is not None
        }
        async with AsyncExitStack() as stack:
            if project_ids:
                for project_id in sorted(project_ids):
                    await stack.enter_async_context(self._fence.writer(project_id))
            else:
                await stack.enter_async_context(self._fence.global_writer())
            await self._inner.batch_upsert(items, collection_name)

    async def set_payload(
        self,
        memory_id: str,
        payload: dict[str, Any],
        collection_name: str | None = None,
    ) -> None:
        async with self._payload_writer(payload):
            await self._inner.set_payload(memory_id, payload, collection_name)

    async def rebuild(self, *args: Any, **kwargs: Any) -> None:
        async with self._fence.global_writer():
            await self._inner.rebuild(*args, **kwargs)

    async def rebuild_from_supplier(
        self,
        memory_supplier: Callable[[], list[dict[str, str]]],
        embed_fn: Any,
    ) -> None:
        """Acquire global admission before capturing the rebuild input snapshot."""
        async with self._fence.global_writer():
            await self._inner.rebuild(memory_supplier(), embed_fn)

    def global_write_context(self) -> AbstractAsyncContextManager[None]:
        """Expose service-level admission for snapshots captured before global writes."""
        return self._fence.global_writer()

    def project_write_context(self, project_id: str) -> AbstractAsyncContextManager[None]:
        """Expose service-level admission spanning compute and project-scoped writes."""
        return self._fence.writer(project_id)

    def _payload_writer(
        self,
        payload: dict[str, Any] | None,
    ) -> AbstractAsyncContextManager[None]:
        project_id = payload.get("project_id") if payload else None
        if project_id is None:
            return self._fence.global_writer()
        return self._fence.writer(str(project_id))
