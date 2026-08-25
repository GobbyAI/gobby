"""Tests for task-scoped memory review tools."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.memory_write import derive_memory_create_provenance
from gobby.storage.memories import MemoryType

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111110042"
PROJECT_ID = "11111111-1111-4111-8111-111111110001"
TASK_ID = "22222222-2222-4222-8222-222222220001"


def _task(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": TASK_ID,
        "project_id": PROJECT_ID,
        "title": "Implement layered memory guidance",
        "seq_num": 42,
        "closed_at": datetime(2026, 8, 25, tzinfo=UTC),
        "closed_in_session_id": SESSION_ID,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _registry(
    *,
    task: SimpleNamespace | None = None,
    candidates: list[SimpleNamespace] | None = None,
) -> tuple[Any, MagicMock, MagicMock]:
    memory_manager = MagicMock()
    memory_manager.search_memories = AsyncMock(return_value=candidates or [])
    task_manager = MagicMock()
    task_manager.get_task.return_value = task
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(id=SESSION_ID, project_id=PROJECT_ID)
    registry = create_memory_registry(
        lambda: memory_manager,
        task_manager=task_manager,
        session_manager=session_manager,
    )
    return registry, memory_manager, task_manager


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [0, 1, 3])
async def test_review_returns_candidates_and_records_success(candidate_count: int) -> None:
    candidates = [
        SimpleNamespace(
            id=f"memory-{index}",
            content=f"Candidate {index}",
            rationale=f"Durable reason {index}",
            memory_type=MemoryType.FACT,
            tags=["architecture"],
            similarity=0.91 - (index * 0.1),
        )
        for index in range(candidate_count)
    ]
    registry, memory_manager, _task_manager = _registry(task=_task(), candidates=candidates)
    state_manager = MagicMock()

    with patch(
        "gobby.mcp_proxy.tools.memory_review.SessionVariableManager",
        return_value=state_manager,
    ):
        result = await registry.call(
            "review_task_memories",
            {
                "task_id": "#42",
                "changes_summary": "Implemented and verified all three memory layers.",
                "session_id": SESSION_ID,
            },
        )

    assert result["success"] is True
    assert result["task_id"] == TASK_ID
    assert result["task_ref"] == "#42"
    assert result["source_task_id"] == TASK_ID
    assert result["candidate_count"] == candidate_count
    assert [candidate["id"] for candidate in result["candidates"]] == [
        f"memory-{index}" for index in range(candidate_count)
    ]
    search_kwargs = memory_manager.search_memories.await_args.kwargs
    assert search_kwargs["query"] == (
        "Implement layered memory guidance\n\nImplemented and verified all three memory layers."
    )
    assert "injected_memory_ids" not in search_kwargs
    assert search_kwargs["project_id"] == PROJECT_ID
    assert search_kwargs["include_global"] is True
    state_manager.upsert_bounded_list_variable.assert_called_once()


@pytest.mark.asyncio
async def test_review_rejects_blank_summary_before_search() -> None:
    registry, memory_manager, _task_manager = _registry(task=_task())

    result = await registry.call(
        "review_task_memories",
        {"task_id": "#42", "changes_summary": "  ", "session_id": SESSION_ID},
    )

    assert result["error"] == "blank_changes_summary"
    memory_manager.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_rejects_missing_identity() -> None:
    registry, memory_manager, _task_manager = _registry(task=_task())

    result = await registry.call(
        "review_task_memories",
        {"task_id": "#42", "changes_summary": "Completed work.", "session_id": ""},
    )

    assert result["error"] == "missing_session_identity"
    memory_manager.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_rejects_unresolved_or_foreign_task() -> None:
    missing_registry, missing_memory, _task_manager = _registry(task=None)
    missing = await missing_registry.call(
        "review_task_memories",
        {"task_id": "#404", "changes_summary": "Completed work.", "session_id": SESSION_ID},
    )

    foreign_registry, foreign_memory, _task_manager = _registry(
        task=_task(closed_in_session_id="33333333-3333-4333-8333-333333330001")
    )
    foreign = await foreign_registry.call(
        "review_task_memories",
        {"task_id": "#42", "changes_summary": "Completed work.", "session_id": SESSION_ID},
    )

    assert missing["error"] == "task_not_found"
    assert foreign["error"] == "foreign_session_closure"
    missing_memory.search_memories.assert_not_awaited()
    foreign_memory.search_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_failure_records_no_review_completion() -> None:
    registry, memory_manager, _task_manager = _registry(task=_task())
    memory_manager.search_memories.side_effect = RuntimeError("embedding service unavailable")
    state_manager_cls = MagicMock()

    with patch(
        "gobby.mcp_proxy.tools.memory_review.SessionVariableManager",
        state_manager_cls,
    ):
        result = await registry.call(
            "review_task_memories",
            {"task_id": "#42", "changes_summary": "Completed work.", "session_id": SESSION_ID},
        )

    assert result["error"] == "memory_search_failed"
    assert "embedding service unavailable" in result["message"]
    state_manager_cls.assert_not_called()


def test_closed_task_is_accepted_as_explicit_create_memory_source() -> None:
    db = MagicMock()
    with patch(
        "gobby.storage.tasks._id.resolve_task_reference",
        return_value=TASK_ID,
    ):
        source_task_id, created_by_agent = derive_memory_create_provenance(
            db,
            project_id=PROJECT_ID,
            resolved_session_id=SESSION_ID,
            source_task_id="#42",
            created_by_agent="codex",
        )

    assert source_task_id == TASK_ID
    assert created_by_agent == "codex"
