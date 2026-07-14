"""Bounds for review-context search fan-out and response size."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.review_learning import create_review_learning_registry
from gobby.review_learning.file_paths import path_tag
from gobby.review_learning.promotion import PromotionTaskManager
from gobby.review_learning.service import (
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
    def __init__(
        self,
        *,
        tagged_batches: list[list[_Memory]],
        legacy: list[_Memory],
    ) -> None:
        self.db = cast(HubDatabase, object())
        self.tagged_batches = iter(tagged_batches)
        self.legacy = legacy
        self.list_calls: list[dict[str, Any]] = []

    async def alist_memories(self, **kwargs: Any) -> list[_Memory]:
        self.list_calls.append(kwargs)
        if len(kwargs["tags_all"]) == 3:
            return next(self.tagged_batches)
        return self.legacy


def _memory_manager() -> tuple[_RecallMemoryManager, ReviewLearningMemoryManager]:
    manager = _RecallMemoryManager()
    return manager, cast(ReviewLearningMemoryManager, manager)


@pytest.mark.asyncio
async def test_candidate_lessons_skip_legacy_scan_when_tagged_candidates_satisfy_limit() -> None:
    first = _Memory(id="first", content="first", tags=[])
    second = _Memory(id="second", content="second", tags=[])
    manager = _CandidateMemoryManager(
        tagged_batches=[[first], [first, second]],
        legacy=[],
    )
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, manager),
        cast(PromotionTaskManager, object()),
    )

    candidates = await service._candidate_lesson_memories(
        project_id="project",
        touched_paths=["src/first.py", "src/second.py"],
        limit=2,
    )

    assert [memory.id for memory, _ in candidates] == ["first", "second"]
    assert len(manager.list_calls) == 2
    assert all(len(call["tags_all"]) == 3 for call in manager.list_calls)


@pytest.mark.asyncio
async def test_candidate_lessons_scan_legacy_when_tagged_candidates_are_insufficient() -> None:
    tagged = _Memory(id="tagged", content="tagged", tags=[])
    legacy = _Memory(id="legacy", content="legacy", tags=[])
    manager = _CandidateMemoryManager(
        tagged_batches=[[tagged]],
        legacy=[legacy],
    )
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, manager),
        cast(PromotionTaskManager, object()),
    )

    candidates = await service._candidate_lesson_memories(
        project_id="project",
        touched_paths=["src/tagged.py"],
        limit=2,
    )

    assert [(memory.id, path) for memory, path in candidates] == [
        ("tagged", "src/tagged.py"),
        ("legacy", None),
    ]
    assert len(manager.list_calls) == 2
    assert len(manager.list_calls[-1]["tags_all"]) == 2


@pytest.mark.asyncio
async def test_candidate_lessons_skip_empty_path_tags() -> None:
    tagged = _Memory(id="tagged", content="tagged", tags=[])
    manager = _CandidateMemoryManager(
        tagged_batches=[[tagged]],
        legacy=[],
    )
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, manager),
        cast(PromotionTaskManager, object()),
    )

    await service._candidate_lesson_memories(
        project_id="project",
        touched_paths=["", "../outside.py", "src/tagged.py"],
        limit=2,
    )

    assert len(manager.list_calls) == 2
    assert manager.list_calls[0]["tags_all"][-1] == path_tag("src/tagged.py")
    assert len(manager.list_calls[-1]["tags_all"]) == 2


@pytest.mark.asyncio
async def test_recall_context_caps_fan_out_and_flat_response() -> None:
    manager, typed_manager = _memory_manager()
    service = ReviewLearningService(
        typed_manager,
        cast(PromotionTaskManager, object()),
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
        typed_manager,
        cast(PromotionTaskManager, object()),
    )

    schema = registry.get_schema("recall_review_context")

    assert schema is not None
    findings_schema = schema["inputSchema"]["properties"]["findings"]
    assert findings_schema["maxItems"] == MAX_RECALL_FINDINGS
    assert str(MAX_RECALL_FINDINGS) in findings_schema["description"]
