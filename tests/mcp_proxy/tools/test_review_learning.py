from __future__ import annotations

import pytest

from gobby.mcp_proxy.tools.review_learning import create_review_learning_registry
from tests.review_learning.conftest import FakeDB, FakeMemoryManager, FakeTaskManager

pytestmark = pytest.mark.unit


def test_create_review_learning_registry_registers_two_tools() -> None:
    registry = create_review_learning_registry(FakeMemoryManager(), FakeTaskManager())

    assert registry.name == "gobby-review-learning"
    tool_names = {tool["name"] for tool in registry.list_tools()}
    assert tool_names == {"recall_review_context", "record_review_lesson"}


@pytest.mark.asyncio
async def test_recall_review_context_groups_matches_per_finding() -> None:
    memory_manager = FakeMemoryManager()
    await memory_manager.create_memory(
        "Local memory",
        tags=["review-lesson", "pattern:example"],
        project_id="_personal",
    )
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "recall_review_context",
        {"findings": [{"title": "Local memory"}]},
    )

    assert result["success"] is True
    assert result["findings"][0]["finding_index"] == 0
    assert result["findings"][0]["matches"][0]["memory_id"] == "mem-1"


@pytest.mark.asyncio
async def test_record_review_lesson_preserves_session_scope() -> None:
    session_id = "11111111-1111-1111-1111-111111111111"
    memory_manager = FakeMemoryManager(db=FakeDB(session_id=session_id, project_id="project-a"))
    registry = create_review_learning_registry(memory_manager, FakeTaskManager())

    result = await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-1",
            "decision": "confirmed",
            "finding": {
                "title": "Reusable finding",
                "pattern_id": "pattern-a",
                "principle": "Prefer local convention",
            },
            "evidence": {"commit": "abc"},
            "session_id": session_id,
        },
    )

    assert result["success"] is True
    assert memory_manager.memories[0].project_id == "project-a"
    assert memory_manager.memories[0].source_session_id == session_id


@pytest.mark.asyncio
async def test_record_review_lesson_handles_stale_invalid_noops() -> None:
    registry = create_review_learning_registry(FakeMemoryManager(), FakeTaskManager())

    result = await registry.call(
        "record_review_lesson",
        {
            "source_kind": "review_comment",
            "source": "coderabbit",
            "source_review": "review-1",
            "decision": "stale",
            "finding": {"title": "stale"},
            "evidence": {},
        },
    )

    assert result["success"] is True
    assert result["skipped_reason"] == "stale"
