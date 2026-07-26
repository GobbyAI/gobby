"""Tests for retry-safe secondary projection scope repair."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.projection_repair import ProjectionScopeRepairService
from gobby.storage.memories_models import MemoryType
from gobby.storage.memories_scope import ALL_MEMORIES
from gobby.storage.projects import GLOBAL_PROJECT_ID


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_repair_uses_authoritative_scope_and_repairs_falkor_in_place() -> None:
    memory = SimpleNamespace(
        id="memory-1",
        content="explicit scope",
        project_id="project-1",
        is_global=False,
        memory_type=MemoryType.FACT,
    )
    storage = MagicMock()
    storage.list_vector_reindex_ids = MagicMock(side_effect=[[memory.id], []])
    storage.get_memories = MagicMock(return_value=[memory])
    storage.list_memories = MagicMock(return_value=[memory])
    restore = AsyncMock(return_value=True)
    falkor = MagicMock()
    falkor.query = AsyncMock(
        side_effect=[
            [{"repaired": 1}],
            [{"repaired": 2}],
            [{"repaired": 3}],
        ]
    )
    service = ProjectionScopeRepairService(
        storage_provider=lambda: storage,
        run_db=_run_db,
        restore_memory_indices=restore,
        falkor_client_provider=lambda: falkor,
    )

    result = await service.repair()

    assert result.vectors_repaired == 1
    assert result.vectors_pending == 0
    assert result.graph_memories_repaired == 1
    assert result.graph_entities_repaired == 5
    assert result.failures == []
    restore.assert_awaited_once_with(
        memory.id,
        memory.content,
        "project-1",
        False,
        memory.memory_type.value,
    )
    storage.get_memories.assert_called_once_with([memory.id], ALL_MEMORIES, visibility="all")
    memory_cypher, memory_params = falkor.query.await_args_list[0].args
    assert "UNWIND $memories AS memory" in memory_cypher
    assert "SET m.project_id = memory.project_id, m.is_global = memory.is_global" in memory_cypher
    assert "DELETE" not in memory_cypher
    assert memory_params == {
        "memories": [
            {
                "memory_id": memory.id,
                "project_id": "project-1",
                "is_global": False,
            }
        ]
    }
    assert falkor.query.await_args_list[1].args[1] == {"global_project_id": GLOBAL_PROJECT_ID}


@pytest.mark.asyncio
async def test_repair_preserves_pending_work_and_reports_retryable_failures() -> None:
    memory = SimpleNamespace(
        id="memory-1",
        content="retry me",
        project_id="project-1",
        is_global=True,
        memory_type=MemoryType.FACT,
    )
    storage = MagicMock()
    storage.list_vector_reindex_ids = MagicMock(return_value=[memory.id])
    storage.get_memories = MagicMock(return_value=[memory])
    restore = AsyncMock(side_effect=RuntimeError("qdrant unavailable"))
    falkor = MagicMock()
    falkor.query = AsyncMock(side_effect=RuntimeError("falkor unavailable"))
    service = ProjectionScopeRepairService(
        storage_provider=lambda: storage,
        run_db=_run_db,
        restore_memory_indices=restore,
        falkor_client_provider=lambda: falkor,
    )

    result = await service.repair()

    assert result.vectors_repaired == 0
    assert result.vectors_pending == 1
    assert result.graph_memories_repaired == 0
    assert result.graph_entities_repaired == 0
    assert result.failures == [
        {
            "memory_id": memory.id,
            "index": "embedding",
            "error": "qdrant unavailable",
        },
        {
            "memory_id": "*",
            "index": "knowledge_graph",
            "error": "falkor unavailable",
        },
    ]


@pytest.mark.asyncio
async def test_graph_repair_paginates_list_memories_with_positive_limit(monkeypatch) -> None:
    """limit=None crashed list_memories; the graph sweep must page instead."""
    from gobby.memory.services import projection_repair as module

    monkeypatch.setattr(module, "_GRAPH_REPAIR_PAGE_SIZE", 2)

    def _memory(idx: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=f"memory-{idx}",
            content=f"content {idx}",
            project_id="project-1",
            is_global=False,
            memory_type=MemoryType.FACT,
        )

    full_page = [_memory(1), _memory(2)]
    partial_page = [_memory(3)]
    storage = MagicMock()
    storage.list_vector_reindex_ids = MagicMock(return_value=[])

    def _list_memories(
        scope: Any = ALL_MEMORIES,
        memory_type: Any = None,
        limit: int = 50,
        offset: int = 0,
        **kwargs: Any,
    ) -> list[SimpleNamespace]:
        # Same guard as the real storage layer: a None limit must never arrive.
        if limit <= 0:
            return []
        return {0: full_page, 2: partial_page}.get(offset, [])

    storage.list_memories = MagicMock(side_effect=_list_memories)
    falkor = MagicMock()

    async def _query(cypher: str, params: dict[str, Any]) -> list[dict[str, int]]:
        if "UNWIND $memories AS memory" in cypher:
            return [{"repaired": len(params["memories"])}]
        return [{"repaired": 1}]

    falkor.query = AsyncMock(side_effect=_query)
    service = ProjectionScopeRepairService(
        storage_provider=lambda: storage,
        run_db=_run_db,
        restore_memory_indices=AsyncMock(return_value=True),
        falkor_client_provider=lambda: falkor,
    )

    result = await service.repair()

    assert result.failures == []
    assert result.graph_memories_repaired == 3
    graph_repair_calls = [
        call
        for call in falkor.query.await_args_list
        if "UNWIND $memories AS memory" in call.args[0]
    ]
    assert len(graph_repair_calls) == 2
    list_calls = storage.list_memories.call_args_list
    assert [call.kwargs["offset"] for call in list_calls] == [0, 2]
    assert all(call.kwargs["limit"] == 2 for call in list_calls)
