"""Tests for retry-safe secondary projection scope repair."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.projection_repair import ProjectionScopeRepairService
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
    restore.assert_awaited_once_with(memory.id, memory.content, "project-1", False)
    storage.get_memories.assert_called_once_with([memory.id], ALL_MEMORIES, visibility="all")
    memory_cypher, memory_params = falkor.query.await_args_list[0].args
    assert "SET m.project_id = $project_id, m.is_global = $is_global" in memory_cypher
    assert "DELETE" not in memory_cypher
    assert memory_params == {
        "memory_id": memory.id,
        "project_id": "project-1",
        "is_global": False,
    }
    assert falkor.query.await_args_list[1].args[1] == {"global_project_id": GLOBAL_PROJECT_ID}


@pytest.mark.asyncio
async def test_repair_preserves_pending_work_and_reports_retryable_failures() -> None:
    memory = SimpleNamespace(
        id="memory-1",
        content="retry me",
        project_id="project-1",
        is_global=True,
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
