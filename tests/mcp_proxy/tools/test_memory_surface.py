"""Tests for the automated memory surfacing tool."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.memory_surface import SURFACE_MIN_SCORE
from gobby.storage.memories import MemoryType

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111110042"
PROJECT_ID = "11111111-1111-4111-8111-111111110001"
RANKING_COHORT = Path(__file__).resolve().parents[2] / "memory" / "fixtures" / "ranking_cohort.json"


def _memory(index: int, **overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": f"002c13ae-000{index}",
        "content": f"Memory {index} content",
        "rationale": f"Memory {index} applies when surfacing",
        "memory_type": MemoryType.PATTERN,
        "updated_at": "2026-09-11T10:00:00+00:00",
        "search_via": "semantic|keyword",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _registry(
    *,
    candidates: list[SimpleNamespace] | None = None,
    search_error: Exception | None = None,
) -> tuple[Any, MagicMock]:
    memory_manager = MagicMock()
    if search_error is not None:
        memory_manager.search_memories = AsyncMock(side_effect=search_error)
    else:
        memory_manager.search_memories = AsyncMock(return_value=candidates or [])
    session = SimpleNamespace(id=SESSION_ID, project_id=PROJECT_ID)
    session_manager = MagicMock()
    session_manager.get.side_effect = lambda session_id: (
        session if session_id == SESSION_ID else None
    )
    registry = create_memory_registry(
        lambda: memory_manager,
        session_manager=session_manager,
    )
    return registry, memory_manager


async def test_surface_search_arguments() -> None:
    registry, memory_manager = _registry(candidates=[_memory(1), _memory(2)])
    # Surrounding whitespace rides along: the search service embeds the text
    # verbatim, so the tool must not reshape it.
    text = "\n  Rebuilding the session handoff contract  \n"

    result = await registry.call(
        "surface_memories",
        {"text": text, "trigger": "spawn_agent", "session_id": SESSION_ID},
    )

    kwargs = memory_manager.search_memories.await_args.kwargs
    assert kwargs["query"] == text
    assert kwargs["embed_text"] == text
    assert kwargs["limit"] == 5
    assert kwargs["tags_none"] == ["review-lesson"]
    assert kwargs["min_score"] == SURFACE_MIN_SCORE
    assert kwargs["caller"] == "memory.surface"
    assert kwargs["session_id"] == SESSION_ID
    assert kwargs["project_id"] == PROJECT_ID
    assert kwargs["recall_request_id"]

    assert result["trigger"] == "spawn_agent"
    assert result["count"] == 2
    assert [memory["id"] for memory in result["memories"]] == [
        "002c13ae-0001",
        "002c13ae-0002",
    ]
    assert result["memories"][0] == {
        "id": "002c13ae-0001",
        "type": "pattern",
        "search_via": "semantic|keyword",
        "updated_at": "2026-09-11T10:00:00+00:00",
        "content": "Memory 1 content",
        "rationale": "Memory 1 applies when surfacing",
    }


async def test_each_surface_call_mints_its_own_recall_request_id() -> None:
    registry, memory_manager = _registry(candidates=[_memory(1)])
    arguments = {"text": "Handoff contract", "trigger": "turn", "session_id": SESSION_ID}

    await registry.call("surface_memories", arguments)
    await registry.call("surface_memories", arguments)

    minted = [
        call.kwargs["recall_request_id"] for call in memory_manager.search_memories.await_args_list
    ]
    assert len(set(minted)) == 2


async def test_surface_truncates_overlong_text() -> None:
    registry, memory_manager = _registry(candidates=[])
    text = "handoff " * 400

    await registry.call(
        "surface_memories",
        {"text": text, "trigger": "handoff", "session_id": SESSION_ID},
    )

    kwargs = memory_manager.search_memories.await_args.kwargs
    assert kwargs["query"] == text[:2000]
    assert kwargs["embed_text"] == kwargs["query"]
    assert len(kwargs["query"]) == 2000


async def test_surface_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    registry, _memory_manager = _registry(search_error=RuntimeError("vector store unreachable"))

    with caplog.at_level(logging.WARNING, logger="gobby.mcp_proxy.tools.memory_surface"):
        result = await registry.call(
            "surface_memories",
            {"text": "Handoff contract", "trigger": "task", "session_id": SESSION_ID},
        )

    assert result["count"] == 0
    assert result["memories"] == []
    assert result["trigger"] == "task"
    record = next(entry for entry in caplog.records if entry.levelno == logging.WARNING)
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)


async def test_surface_returns_nothing_for_an_unresolvable_session() -> None:
    registry, memory_manager = _registry(candidates=[_memory(1)])

    with patch(
        "gobby.mcp_proxy.tools.memory_surface.resolve_session_reference",
        side_effect=ValueError("Session 'nope' not found"),
    ):
        result = await registry.call(
            "surface_memories",
            {"text": "Handoff contract", "trigger": "turn", "session_id": "nope"},
        )

    assert result["count"] == 0
    assert result["memories"] == []
    memory_manager.search_memories.assert_not_awaited()


def test_floor_matches_ranking_fixture() -> None:
    cohort = json.loads(RANKING_COHORT.read_text())

    assert SURFACE_MIN_SCORE == cohort["surface_min_score"]
