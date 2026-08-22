"""Bounds for review-context search fan-out and response size."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.review_learning import create_review_learning_registry
from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.file_paths import path_tag
from gobby.review_learning.service import (
    _LEGACY_SCAN_LIMIT,
    MAX_RECALL_FINDINGS,
    MAX_RECALL_FLAT_MATCHES,
    ReviewLearningMemoryManager,
    ReviewLearningService,
)
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@dataclass
class _Memory:
    id: str
    content: str
    tags: list[str]


class _RecallMemoryManager:
    def __init__(self) -> None:
        self.db = cast(HubDatabase, object())
        self.search_calls: list[dict[str, Any]] = []
        self.ordinary = [
            _Memory(id=f"ordinary-{index}", content=f"ordinary {index}", tags=[])
            for index in range(5)
        ]
        self.lessons = [
            _Memory(
                id=f"lesson-{index}",
                content=f"lesson {index}",
                tags=["review-lesson"],
            )
            for index in range(5)
        ]

    async def search_memories(self, **kwargs: Any) -> list[_Memory]:
        self.search_calls.append(kwargs)
        return self.lessons if kwargs.get("tags_all") else self.ordinary


class _CandidateMemoryManager:
    def __init__(self, memories: list[_Memory]) -> None:
        self.db = cast(HubDatabase, object())
        self.memories = memories
        self.list_calls: list[dict[str, Any]] = []

    async def alist_memories(self, **kwargs: Any) -> list[_Memory]:
        self.list_calls.append(kwargs)
        return self.memories


def _memory_manager() -> tuple[_RecallMemoryManager, ReviewLearningMemoryManager]:
    manager = _RecallMemoryManager()
    return manager, cast(ReviewLearningMemoryManager, manager)


@pytest.mark.asyncio
async def test_candidate_lessons_use_one_bounded_scan_and_rank_tagged_first() -> None:
    tagged = _Memory(
        id="tagged",
        content="tagged",
        tags=["review-lesson", "confirmed", path_tag("src/second.py")],
    )
    untagged = _Memory(id="untagged", content="untagged", tags=["review-lesson", "confirmed"])
    manager = _CandidateMemoryManager([untagged, tagged])
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, manager),
        cast(RetirementTaskManager, object()),
    )

    candidates = await service._candidate_lesson_memories(
        project_id="project",
        touched_paths=["src/first.py", "src/second.py"],
        limit=2,
    )

    assert [(memory.id, path) for memory, path in candidates] == [
        ("tagged", "src/second.py"),
        ("untagged", None),
    ]
    assert len(manager.list_calls) == 1
    call = manager.list_calls[0]
    assert call["tags_all"] == ["review-lesson", "confirmed"]
    assert call["limit"] == _LEGACY_SCAN_LIMIT
    assert call["include_global"] is False


@pytest.mark.asyncio
async def test_candidate_lessons_deduplicate_by_memory_id() -> None:
    first = _Memory(
        id="lesson",
        content="lesson",
        tags=["review-lesson", "confirmed", path_tag("src/tagged.py")],
    )
    duplicate = _Memory(
        id="lesson",
        content="lesson",
        tags=["review-lesson", "confirmed", path_tag("src/tagged.py")],
    )
    manager = _CandidateMemoryManager([first, duplicate])
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, manager),
        cast(RetirementTaskManager, object()),
    )

    candidates = await service._candidate_lesson_memories(
        project_id="project",
        touched_paths=["src/tagged.py"],
        limit=2,
    )

    assert [(memory.id, path) for memory, path in candidates] == [("lesson", "src/tagged.py")]
    assert len(manager.list_calls) == 1


@pytest.mark.asyncio
async def test_candidate_lessons_skip_empty_path_tags() -> None:
    tagged = _Memory(
        id="tagged",
        content="tagged",
        tags=["review-lesson", "confirmed", path_tag("src/tagged.py")],
    )
    manager = _CandidateMemoryManager([tagged])
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, manager),
        cast(RetirementTaskManager, object()),
    )

    candidates = await service._candidate_lesson_memories(
        project_id="project",
        touched_paths=["", "../outside.py", "src/tagged.py"],
        limit=2,
    )

    assert [(memory.id, path) for memory, path in candidates] == [("tagged", "src/tagged.py")]
    assert len(manager.list_calls) == 1


@pytest.mark.asyncio
async def test_recall_context_caps_fan_out_and_flat_response() -> None:
    manager, typed_manager = _memory_manager()
    service = ReviewLearningService(
        typed_manager,
        cast(RetirementTaskManager, object()),
    )
    findings: list[dict[str, Any] | str] = [
        {"message": f"finding-{index} {'x' * 300}"} for index in range(MAX_RECALL_FINDINGS + 5)
    ]

    result = await service.recall_context(findings=findings)

    assert len(manager.search_calls) == MAX_RECALL_FINDINGS * 4
    assert len(result["findings"]) == MAX_RECALL_FINDINGS
    assert all(len(group["matches"]) == 10 for group in result["findings"])
    assert len(result["matches"]) == MAX_RECALL_FLAT_MATCHES


def test_recall_context_schema_declares_findings_max_items() -> None:
    _, typed_manager = _memory_manager()
    registry = create_review_learning_registry(
        ReviewLearningService(
            typed_manager,
            cast(RetirementTaskManager, object()),
        )
    )

    schema = registry.get_schema("recall_review_context")

    assert schema is not None
    findings_schema = schema["inputSchema"]["properties"]["findings"]
    assert findings_schema["maxItems"] == MAX_RECALL_FINDINGS
    assert str(MAX_RECALL_FINDINGS) in findings_schema["description"]
