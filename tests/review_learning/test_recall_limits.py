"""Bounds for review-context search fan-out and response size."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.review_learning import create_review_learning_registry
from gobby.review_learning import lessons as lessons_module
from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.file_paths import path_tag
from gobby.review_learning.lessons import CODE_DOMAIN_EXCLUDED_TAGS
from gobby.review_learning.service import (
    _CANDIDATE_OVERFETCH,
    MAX_RECALL_FINDINGS,
    MAX_RECALL_FLAT_MATCHES,
    ReviewLearningMemoryManager,
    ReviewLearningService,
)
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

# The literal the writer stamps; `test_unscoped_scope_tag_is_the_expected_literal`
# pins the production constant to it.
UNSCOPED_SCOPE_TAG = "scope:unscoped"


def test_unscoped_scope_tag_is_the_expected_literal() -> None:
    assert lessons_module.UNSCOPED_SCOPE_TAG == UNSCOPED_SCOPE_TAG


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
    """Applies the tag filters Postgres applies, so `limit` bounds real rows."""

    def __init__(self, memories: list[_Memory]) -> None:
        self.db = cast(HubDatabase, object())
        self.memories = memories
        self.list_calls: list[dict[str, Any]] = []

    async def alist_memories(self, **kwargs: Any) -> list[_Memory]:
        self.list_calls.append(kwargs)
        tags_all = kwargs.get("tags_all") or []
        tags_any = kwargs.get("tags_any")
        tags_none = kwargs.get("tags_none") or []
        matched = [
            memory
            for memory in self.memories
            if set(tags_all).issubset(memory.tags)
            and (tags_any is None or set(tags_any).intersection(memory.tags))
            and not set(tags_none).intersection(memory.tags)
        ]
        limit = kwargs.get("limit")
        return matched if limit is None else matched[:limit]


def _memory_manager() -> tuple[_RecallMemoryManager, ReviewLearningMemoryManager]:
    manager = _RecallMemoryManager()
    return manager, cast(ReviewLearningMemoryManager, manager)


@pytest.mark.asyncio
async def test_candidate_lessons_use_bounded_queries_and_rank_tagged_first() -> None:
    """1.4.2: the fixed 200-row page is replaced by two queries bounded by `limit`."""
    tagged = _Memory(
        id="tagged",
        content="tagged",
        tags=["review-lesson", "confirmed", path_tag("src/second.py")],
    )
    unscoped = _Memory(
        id="unscoped",
        content="unscoped",
        tags=["review-lesson", "confirmed", UNSCOPED_SCOPE_TAG],
    )
    manager = _CandidateMemoryManager([unscoped, tagged])
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
        ("unscoped", None),
    ]
    assert len(manager.list_calls) == 2
    path_call, unscoped_call = manager.list_calls
    assert path_call["tags_all"] == ["review-lesson", "confirmed"]
    assert set(path_call["tags_any"]) == {path_tag("src/first.py"), path_tag("src/second.py")}
    assert unscoped_call["tags_all"] == ["review-lesson", "confirmed", UNSCOPED_SCOPE_TAG]
    assert unscoped_call.get("tags_any") is None
    for call in manager.list_calls:
        assert call["limit"] == 2 * _CANDIDATE_OVERFETCH
        assert call["include_global"] is False
        assert call["memory_type"] == "pattern"
        assert call["tags_none"] == list(CODE_DOMAIN_EXCLUDED_TAGS)


@pytest.mark.asyncio
async def test_path_matched_precede_unscoped_within_limit() -> None:
    """1.4.5: path-matched lessons rank first and neither query exceeds its bound.

    The bound is `limit * _CANDIDATE_OVERFETCH` rather than `limit`: recall drops
    non-actionable lessons after the fetch, so a page of exactly `limit` can yield
    nothing. The pool stays constant as the corpus grows, which is what the fixed
    200-row page failed to do.
    """
    memories = [
        _Memory(
            id=f"unscoped-{index}",
            content="unscoped",
            tags=["review-lesson", "confirmed", UNSCOPED_SCOPE_TAG],
        )
        for index in range(5)
    ] + [
        _Memory(
            id=f"matched-{index}",
            content="matched",
            tags=["review-lesson", "confirmed", path_tag("src/touched.py")],
        )
        for index in range(5)
    ]
    manager = _CandidateMemoryManager(memories)
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, manager),
        cast(RetirementTaskManager, object()),
    )

    candidates = await service._candidate_lesson_memories(
        project_id="project",
        touched_paths=["src/touched.py"],
        limit=2,
    )

    ids = [memory.id for memory, _ in candidates]
    paths = [path for _, path in candidates]
    matched_positions = [index for index, path in enumerate(paths) if path is not None]
    unscoped_positions = [index for index, path in enumerate(paths) if path is None]

    assert all(identifier.startswith("matched-") for identifier in ids[: len(matched_positions)])
    assert max(matched_positions) < min(unscoped_positions)
    assert all(paths[index] == "src/touched.py" for index in matched_positions)
    # Neither query may exceed the bounded candidate pool, whatever the corpus size.
    assert len(manager.list_calls) == 2
    assert all(call["limit"] == 2 * _CANDIDATE_OVERFETCH for call in manager.list_calls)
    assert len(candidates) <= 2 * 2 * _CANDIDATE_OVERFETCH


@pytest.mark.asyncio
async def test_unscoped_lesson_reachable_beyond_legacy_page() -> None:
    """1.4.6: an unscoped lesson stays reachable however large the corpus grows.

    The corpus is deliberately larger than the retired 200-row page, and the
    unscoped lesson sits past its end, where a truncated scan would lose it.
    """
    corpus = [
        _Memory(
            id=f"other-{index}",
            content="other",
            tags=["review-lesson", "confirmed", path_tag(f"src/other_{index}.py")],
        )
        for index in range(400)
    ]
    corpus.append(
        _Memory(
            id="unscoped",
            content="applies everywhere",
            tags=["review-lesson", "confirmed", UNSCOPED_SCOPE_TAG],
        )
    )
    manager = _CandidateMemoryManager(corpus)
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, manager),
        cast(RetirementTaskManager, object()),
    )

    candidates = await service._candidate_lesson_memories(
        project_id="project",
        touched_paths=["src/untouched_by_any_lesson.py"],
        limit=3,
    )

    assert [memory.id for memory, _ in candidates] == ["unscoped"]


@pytest.mark.asyncio
async def test_path_scoped_lesson_does_not_surface_for_an_unrelated_file() -> None:
    """The stated behavior change: `path:X` lessons stop leaking into edits of `Y`."""
    other = _Memory(
        id="other",
        content="scoped elsewhere",
        tags=["review-lesson", "confirmed", path_tag("src/elsewhere.py")],
    )
    manager = _CandidateMemoryManager([other])
    service = ReviewLearningService(
        cast(ReviewLearningMemoryManager, manager),
        cast(RetirementTaskManager, object()),
    )

    candidates = await service._candidate_lesson_memories(
        project_id="project",
        touched_paths=["src/touched.py"],
        limit=3,
    )

    assert candidates == []


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
    assert len(manager.list_calls) == 2


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
    assert len(manager.list_calls) == 2
    assert manager.list_calls[0]["tags_any"] == [path_tag("src/tagged.py")]


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
