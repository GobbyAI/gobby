"""Rebuild and maintenance operations for :mod:`gobby.memory.vectorstore`."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from qdrant_client.http.models.models import (
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
)

from gobby.memory.embedding_text import memory_embedding_text
from gobby.memory.vectorstore_client import QdrantClientLike, VectorStoreCollectionDimensionError
from gobby.memory.vectorstore_rebuild import RebuildCollectionPlan

if TYPE_CHECKING:
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)

StaleDeleteStrategy = Literal["precompute", "streaming"]


class VectorStoreMaintenance:
    """Own collection rebuild, activation, and stale-point cleanup."""

    def __init__(self, store: VectorStore) -> None:
        self._store = store

    async def prepare_collection_for_rebuild(
        self,
        client: QdrantClientLike,
        *,
        recreate_on_mismatch: bool = True,
    ) -> RebuildCollectionPlan:
        """Choose a rebuild target without modifying the active collection."""
        store = self._store
        try:
            aliases_response = await store._call_client(client, "get_aliases")
            alias_targets = {
                alias.alias_name: alias.collection_name for alias in aliases_response.aliases
            }
            active_alias_target = alias_targets.get(store._collection_name)
            exists = await store._call_client(
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
                        "Created Qdrant collection '%s' for rebuild (dim=%s)",
                        store._collection_name,
                        store._embedding_dim,
                    )
                return RebuildCollectionPlan(
                    target_name=store._collection_name,
                    target_is_empty=created,
                )

            existing_dim = await store._read_collection_dimension(
                client,
                store._collection_name,
            )
            if existing_dim is not None and existing_dim != store._embedding_dim:
                if not recreate_on_mismatch:
                    raise VectorStoreCollectionDimensionError(
                        f"Qdrant collection '{store._collection_name}' dimension mismatch "
                        f"(expected_dim={store._embedding_dim}, observed_dim={existing_dim})"
                    )
                target_name = f"{store._collection_name}@rebuild-{time.time_ns()}"
                created = await store._create_collection(
                    client,
                    target_name,
                    store._embedding_dim,
                )
                if not created:
                    raise RuntimeError(f"Could not create rebuild collection '{target_name}'")
                logger.info(
                    "Created temporary Qdrant collection '%s' for dimension change %s->%s",
                    target_name,
                    existing_dim,
                    store._embedding_dim,
                )
                return RebuildCollectionPlan(
                    target_name=target_name,
                    target_is_empty=True,
                    active_target=active_alias_target or store._collection_name,
                    active_is_alias=active_alias_target is not None,
                )
        except Exception as exc:
            store._raise_if_recoverable(exc)
            raise
        return RebuildCollectionPlan(
            target_name=store._collection_name,
            target_is_empty=False,
        )

    async def activate_rebuild_collection(
        self,
        client: QdrantClientLike,
        plan: RebuildCollectionPlan,
    ) -> None:
        """Activate a fully populated rebuild target."""
        store = self._store
        operations: list[DeleteAliasOperation | CreateAliasOperation] = []
        if plan.active_is_alias:
            operations.append(
                DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=store._collection_name))
            )
        else:
            await store._call_client(
                client,
                "delete_collection",
                collection_name=store._collection_name,
            )
        operations.append(
            CreateAliasOperation(
                create_alias=CreateAlias(
                    collection_name=plan.target_name,
                    alias_name=store._collection_name,
                )
            )
        )
        await store._call_client(
            client,
            "update_collection_aliases",
            change_aliases_operations=operations,
        )

    async def delete_collection_best_effort(
        self,
        client: QdrantClientLike,
        collection_name: str,
    ) -> None:
        try:
            await self._store._call_client(
                client,
                "delete_collection",
                collection_name=collection_name,
            )
        except Exception as exc:
            logger.warning("Could not delete obsolete collection '%s': %s", collection_name, exc)

    async def rebuild(
        self,
        memories: list[dict[str, Any]],
        embed_fn: Callable[[str], Awaitable[list[float]]],
        *,
        recreate_on_mismatch: bool = True,
        stale_delete_strategy: StaleDeleteStrategy = "precompute",
    ) -> None:
        """Re-embed memories, replace stale points, and activate dimension changes."""
        if stale_delete_strategy not in ("precompute", "streaming"):
            raise ValueError("stale_delete_strategy must be 'precompute' or 'streaming'")
        store = self._store
        async with store._rebuild_lock:
            client = await store._ensure_initialized()
            async with store._collection_lifecycle_lock:
                plan = await store._prepare_collection_for_rebuild(
                    client,
                    recreate_on_mismatch=recreate_on_mismatch,
                )
                activation_started = False
                try:
                    batch_size = 500
                    total = 0
                    incoming_ids: set[str] = (
                        {str(memory["id"]) for memory in memories}
                        if stale_delete_strategy == "precompute"
                        else set()
                    )
                    batch: list[tuple[str, list[float], dict[str, Any]]] = []
                    for memory in memories:
                        memory_id = str(memory["id"])
                        if stale_delete_strategy == "streaming":
                            incoming_ids.add(memory_id)
                        embedding = await embed_fn(
                            memory_embedding_text(memory["content"], memory.get("rationale"))
                        )
                        payload = {
                            key: value
                            for key, value in memory.items()
                            if key not in ("id", "rationale")
                        }
                        batch.append((memory_id, embedding, payload))
                        if len(batch) >= batch_size:
                            await store._queries.batch_upsert(
                                batch,
                                collection_name=plan.target_name,
                                client=client,
                            )
                            total += len(batch)
                            logger.info("Rebuild progress: %s/%s vectors", total, len(memories))
                            batch = []

                    if batch:
                        await store._queries.batch_upsert(
                            batch,
                            collection_name=plan.target_name,
                            client=client,
                        )
                        total += len(batch)

                    if not plan.target_is_empty:
                        await store._delete_stale_ids(
                            client,
                            incoming_ids,
                            batch_size=batch_size,
                        )
                    if plan.requires_swap:
                        activation_started = True
                        await store._activate_rebuild_collection(client, plan)
                except BaseException:
                    if plan.requires_swap and not activation_started:
                        await store._delete_collection_best_effort(client, plan.target_name)
                    raise

                if plan.active_is_alias and plan.active_target is not None:
                    await store._delete_collection_best_effort(client, plan.active_target)
                store._status.mark_rebuild_complete()
                logger.info("Rebuilt %s vectors in '%s'", total, store._collection_name)

    async def delete_stale_ids(
        self,
        client: QdrantClientLike,
        incoming_ids: set[str],
        *,
        batch_size: int,
    ) -> None:
        """Delete point IDs absent from the incoming rebuild set."""
        store = self._store
        offset = None
        stale_ids: list[str] = []
        while True:
            try:
                points, next_offset = await store._call_client(
                    client,
                    "scroll",
                    collection_name=store._collection_name,
                    limit=batch_size,
                    offset=offset,
                    with_payload=False,
                    with_vectors=False,
                )
            except Exception as exc:
                store._raise_if_recoverable(exc)
                raise
            for point in points:
                point_id = str(point.id)
                if point_id not in incoming_ids:
                    stale_ids.append(point_id)
            if next_offset is None:
                break
            offset = next_offset
        for index in range(0, len(stale_ids), batch_size):
            await store.delete_many(stale_ids[index : index + batch_size])
        logger.info("Deleted %s stale points from '%s'", len(stale_ids), store._collection_name)
