"""Idempotent repair of memory scope metadata in secondary projections."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from gobby.storage.memories import LocalMemoryManager, Memory
from gobby.storage.memories_scope import ALL_MEMORIES
from gobby.storage.projects import GLOBAL_PROJECT_ID

logger = logging.getLogger(__name__)

# list_memories requires a positive limit (limit=None raises), so the graph
# repair walks the full set in pages.
_GRAPH_REPAIR_PAGE_SIZE = 500


class FalkorQueryProtocol(Protocol):
    async def query(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ProjectionScopeRepairResult:
    """Outcome of one secondary-projection scope repair pass."""

    vectors_repaired: int = 0
    vectors_pending: int = 0
    graph_memories_repaired: int = 0
    graph_entities_repaired: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)


class ProjectionScopeRepairService:
    """Bring Qdrant and Falkor scope metadata in line with PostgreSQL."""

    def __init__(
        self,
        *,
        storage_provider: Callable[[], LocalMemoryManager],
        run_db: Callable[..., Awaitable[Any]],
        restore_memory_indices: Callable[[str, str, str, bool, str], Awaitable[bool]],
        falkor_client_provider: Callable[[], FalkorQueryProtocol | None],
    ) -> None:
        self._storage_provider = storage_provider
        self._run_db = run_db
        self._restore_memory_indices = restore_memory_indices
        self._falkor_client_provider = falkor_client_provider

    @property
    def storage(self) -> LocalMemoryManager:
        return self._storage_provider()

    async def repair(self) -> ProjectionScopeRepairResult:
        """Run one retry-safe repair pass without changing authoritative scope."""
        failures: list[dict[str, str]] = []
        memories = await self._pending_vector_memories()
        vectors_repaired = 0
        for memory in memories:
            try:
                if await self._restore_memory_indices(
                    memory.id,
                    memory.content,
                    memory.project_id,
                    memory.is_global,
                    memory.memory_type.value,
                ):
                    vectors_repaired += 1
            except Exception as exc:
                logger.warning("Memory vector scope repair failed for %s: %s", memory.id, exc)
                failures.append({"memory_id": memory.id, "index": "embedding", "error": str(exc)})

        graph_memories_repaired = 0
        graph_entities_repaired = 0
        falkor = self._falkor_client_provider()
        if falkor is not None:
            try:
                graph_memories_repaired = await self._repair_falkor_memories(falkor)
                graph_entities_repaired = await self._repair_falkor_entities(falkor)
            except Exception as exc:
                logger.warning("Memory graph scope repair failed: %s", exc)
                failures.append({"memory_id": "*", "index": "knowledge_graph", "error": str(exc)})

        pending_ids = await self._run_db(self.storage.list_vector_reindex_ids)
        return ProjectionScopeRepairResult(
            vectors_repaired=vectors_repaired,
            vectors_pending=len(pending_ids),
            graph_memories_repaired=graph_memories_repaired,
            graph_entities_repaired=graph_entities_repaired,
            failures=failures,
        )

    async def _pending_vector_memories(self) -> list[Memory]:
        memory_ids = await self._run_db(self.storage.list_vector_reindex_ids)
        if not memory_ids:
            return []
        return cast(
            list[Memory],
            await self._run_db(
                self.storage.get_memories,
                memory_ids,
                ALL_MEMORIES,
                visibility="all",
            ),
        )

    async def _repair_falkor_memories(self, falkor: FalkorQueryProtocol) -> int:
        repaired = 0
        offset = 0
        while True:
            memories = await self._run_db(
                self.storage.list_memories,
                scope=ALL_MEMORIES,
                limit=_GRAPH_REPAIR_PAGE_SIZE,
                offset=offset,
                visibility="all",
            )
            if memories:
                rows = await falkor.query(
                    """
                    UNWIND $memories AS memory
                    MATCH (m:Memory {memory_id: memory.memory_id})
                    WHERE NOT exists(m.project_id)
                       OR NOT exists(m.is_global)
                       OR m.project_id <> memory.project_id
                       OR m.is_global <> memory.is_global
                    SET m.project_id = memory.project_id, m.is_global = memory.is_global
                    RETURN count(m) AS repaired
                    """,
                    {
                        "memories": [
                            {
                                "memory_id": memory.id,
                                "project_id": memory.project_id,
                                "is_global": memory.is_global,
                            }
                            for memory in memories
                        ],
                    },
                )
                if rows:
                    repaired += int(rows[0].get("repaired", 0))
            if len(memories) < _GRAPH_REPAIR_PAGE_SIZE:
                return repaired
            offset += len(memories)

    @staticmethod
    async def _repair_falkor_entities(falkor: FalkorQueryProtocol) -> int:
        global_rows = await falkor.query(
            """
            MATCH (e:_Entity)
            WHERE NOT exists(e.project_id)
            SET e.project_id = $global_project_id, e.is_global = true
            RETURN count(e) AS repaired
            """,
            {"global_project_id": GLOBAL_PROJECT_ID},
        )
        explicit_rows = await falkor.query(
            """
            MATCH (e:_Entity)
            WHERE exists(e.project_id) AND NOT exists(e.is_global)
            SET e.is_global = CASE
                WHEN e.project_id = $global_project_id THEN true
                ELSE false
            END
            RETURN count(e) AS repaired
            """,
            {"global_project_id": GLOBAL_PROJECT_ID},
        )
        return sum(int(rows[0].get("repaired", 0)) for rows in (global_rows, explicit_rows) if rows)
