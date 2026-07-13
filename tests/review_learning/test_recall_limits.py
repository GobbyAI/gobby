"""Bounds for review-context search fan-out and response size."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.review_learning import create_review_learning_registry
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


def _memory_manager() -> tuple[_RecallMemoryManager, ReviewLearningMemoryManager]:
    manager = _RecallMemoryManager()
    return manager, cast(ReviewLearningMemoryManager, manager)


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
