"""Regression coverage for memory-only review learning."""

from __future__ import annotations

from typing import Any, cast

import pytest

from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.service import ReviewLearningMemoryManager, ReviewLearningService
from tests.review_learning.conftest import FakeMemoryManager, FakeTaskManager

pytestmark = pytest.mark.unit


def _finding(*, target: str) -> dict[str, str]:
    return {
        "title": "Preserve evidence across state transitions",
        "lesson_type": "state-evidence",
        "pattern_id": "state-evidence-transition",
        "principle": "Every state transition defines its evidence policy.",
        "prevention": "Check whether each transition clears, preserves, or replaces evidence.",
        "path": "src/gobby/example.py",
        "guardrail_target": target,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["confirmed", "no-fix-policy"])
async def test_repeated_lessons_never_create_or_update_tasks(decision: str) -> None:
    memory_manager = FakeMemoryManager()
    task_manager = FakeTaskManager()
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, memory_manager),
        cast(RetirementTaskManager, task_manager),
    )

    results = [
        await service.record(
            source_kind="agent_review",
            source="reviewer",
            source_review=f"review-{index}",
            decision=decision,
            finding=_finding(target="checklist"),
            evidence={"proof": f"occurrence-{index}"},
        )
        for index in range(1, 4)
    ]

    assert len(memory_manager.memories) == 3
    assert task_manager.created == []
    assert task_manager.updated == []
    assert all("task_ref" not in result for result in results)
    assert all("guardrail_target" not in result for result in results)


@pytest.mark.asyncio
async def test_duplicate_occurrence_returns_existing_memory_without_task_activity() -> None:
    memory_manager = FakeMemoryManager()
    task_manager = FakeTaskManager()
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, memory_manager),
        cast(RetirementTaskManager, task_manager),
    )
    arguments: dict[str, Any] = {
        "source_kind": "agent_review",
        "source": "reviewer",
        "source_review": "review-1",
        "decision": "confirmed",
        "finding": _finding(target="validation"),
        "evidence": {"proof": "same occurrence"},
    }

    first = await service.record(**arguments)
    duplicate = await service.record(**arguments)

    assert duplicate["lesson_id"] == first["lesson_id"]
    assert duplicate["skipped_reason"] == "duplicate_occurrence"
    assert len(memory_manager.memories) == 1
    assert task_manager.created == []
    assert task_manager.updated == []
