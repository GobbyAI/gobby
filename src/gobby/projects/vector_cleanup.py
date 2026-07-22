"""Purge project-scoped points from every managed embedding collection."""

from __future__ import annotations

import asyncio

from gobby.memory.collection_names import CollectionNameResolver
from gobby.memory.vectorstore import VectorStore


class ProjectVectorCleaner:
    """Remove project payloads, including staged switch collections."""

    def __init__(
        self,
        vector_store: VectorStore,
        *,
        names: CollectionNameResolver | None = None,
    ) -> None:
        self._vector_store = vector_store
        self._names = names or CollectionNameResolver()

    async def clear_project(self, project_id: str, memory_ids: list[str]) -> None:
        for collection_name in await self._managed_physical_collections():
            await self._vector_store.delete(
                filters={"project_id": project_id},
                collection_name=collection_name,
            )
            if memory_ids:
                await self._vector_store.delete_many(
                    memory_ids,
                    collection_name=collection_name,
                )

    async def _managed_physical_collections(self) -> list[str]:
        client = await self._vector_store._ensure_initialized()
        response = await asyncio.to_thread(client.get_collections)
        physical_names = {str(item.name) for item in response.collections}
        aliases = await self._vector_store.get_aliases()
        managed: set[str] = set()
        for name in physical_names:
            parsed = self._names.parse_physical_name(name)
            kind = parsed[0] if parsed is not None else name
            if kind in self._names.kinds:
                managed.add(name)
        for alias, target in aliases.items():
            if alias in self._names.kinds and target in physical_names:
                managed.add(target)
        return sorted(managed)
