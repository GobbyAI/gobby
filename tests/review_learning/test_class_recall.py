from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from gobby.review_learning.class_recall import RetirementTaskManager
from gobby.review_learning.lessons import normalize_lesson
from gobby.review_learning.service import ReviewLearningMemoryManager, ReviewLearningService
from tests.review_learning.conftest import (
    PROJECT_SCOPE_ID,
    FakeMemory,
    FakeMemoryManager,
    FakeTaskManager,
)

pytestmark = pytest.mark.unit


def _lesson_memory(
    *,
    memory_id: str,
    source_kind: str,
    lesson_type: str,
    pattern_id: str,
    check_key: str,
    created_at: datetime,
    occurrence: str,
    category: str | None = None,
    project_id: str | None = PROJECT_SCOPE_ID,
    principle: str | None = None,
    prevention: str | None = None,
) -> FakeMemory:
    finding = {
        "title": f"Lesson {memory_id}",
        "lesson_type": lesson_type,
        "pattern_id": pattern_id,
        "check_key": check_key,
        "principle": f"Principle {memory_id}" if principle is None else principle,
        "prevention": (
            f"Do the safe thing for {memory_id}. Avoid the unsafe thing."
            if prevention is None
            else prevention
        ),
    }
    if category is not None:
        finding["category"] = category
    lesson = normalize_lesson(
        source_kind=source_kind,
        source="test",
        source_review=f"review-{occurrence}",
        decision="confirmed",
        finding=finding,
        evidence={},
        finding_fingerprint=f"fingerprint-{occurrence}",
        occurrence_key=f"occurrence-{occurrence}",
        repo=None,
        language=None,
        risk="medium",
    )
    memory = FakeMemory(
        id=memory_id,
        content=lesson.content,
        project_id=project_id,
        tags=lesson.tags,
        created_at=created_at,
    )
    return memory


def _service(memory_manager: FakeMemoryManager) -> ReviewLearningService:
    return ReviewLearningService(
        memory_manager=cast(ReviewLearningMemoryManager, memory_manager),
        task_manager=cast(RetirementTaskManager, FakeTaskManager()),
    )


@pytest.mark.asyncio
async def test_domain_required_and_consistent(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(FakeMemoryManager())

    for lesson_domain in ("", "unknown"):
        with pytest.raises(ValueError, match="lesson_domain"):
            await service.recall_review_lessons_by_class(
                lesson_domain=lesson_domain,
                lesson_types=["missing-check"],
            )

    with pytest.raises(ValueError, match="source kind"):
        await service.recall_review_lessons_by_class(
            lesson_domain="code",
            lesson_types=["missing-check"],
            source_kinds=["unknown"],
        )

    with pytest.raises(ValueError, match="does not belong"):
        await service.recall_review_lessons_by_class(
            lesson_domain="code",
            lesson_types=["missing-check"],
            source_kinds=["plan_review"],
        )

    monkeypatch.setattr("gobby.review_learning.service.get_project_context", lambda: None)
    monkeypatch.setattr("gobby.review_learning.service.get_current_session_id", lambda: None)
    with pytest.raises(RuntimeError, match="project context"):
        await service.recall_review_lessons_by_class(
            lesson_domain="code",
            lesson_types=["missing-check"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_limit", [None, "three", object()])
async def test_limit_rejects_non_numeric_values(invalid_limit: object) -> None:
    service = _service(FakeMemoryManager())

    with pytest.raises(ValueError, match="limit must be a numeric value"):
        await service.recall_review_lessons_by_class(
            lesson_domain="code",
            lesson_types=["missing-check"],
            limit=cast(int, invalid_limit),
        )


@pytest.mark.asyncio
async def test_cross_domain_same_lesson_type() -> None:
    now = datetime(2026, 7, 23, tzinfo=UTC)
    memory_manager = FakeMemoryManager()
    memory_manager.memories = [
        _lesson_memory(
            memory_id="plan-lesson",
            source_kind="plan_review",
            lesson_type="missing-check",
            pattern_id="plan-review:missing-check:correctness:plan-key",
            check_key="plan-key",
            category="correctness",
            created_at=now,
            occurrence="plan",
        ),
        _lesson_memory(
            memory_id="code-lesson",
            source_kind="task_validation",
            lesson_type="missing-check",
            pattern_id="epic-qa:missing-check:code-key",
            check_key="code-key",
            created_at=now,
            occurrence="code",
        ),
        _lesson_memory(
            memory_id="global-code-lesson",
            source_kind="task_validation",
            lesson_type="missing-check",
            pattern_id="epic-qa:missing-check:global-code-key",
            check_key="global-code-key",
            created_at=now,
            occurrence="global-code",
            project_id=None,
        ),
    ]

    result = await _service(memory_manager).recall_review_lessons_by_class(
        lesson_domain="code",
        lesson_types=["missing-check"],
    )

    assert result["count"] == 1
    assert [lesson["memory_id"] for lesson in result["lessons"]] == ["code-lesson"]
    assert "matched lesson class" in result["message"]
    assert "plan-lesson" not in result["message"]

    empty_sources = await _service(memory_manager).recall_review_lessons_by_class(
        lesson_domain="code",
        lesson_types=["missing-check"],
        source_kinds=[],
    )
    assert empty_sources["count"] == 0


@pytest.mark.asyncio
async def test_dedupe_and_deterministic_ranking() -> None:
    now = datetime(2026, 7, 23, tzinfo=UTC)
    memories = [
        _lesson_memory(
            memory_id="z-pattern-a-old",
            source_kind="task_validation",
            lesson_type="missing-check",
            pattern_id="epic-qa:missing-check:pattern-a",
            check_key="pattern-a",
            created_at=now - timedelta(days=1),
            occurrence="a-1",
        ),
        _lesson_memory(
            memory_id="a-pattern-a-new",
            source_kind="task_validation",
            lesson_type="missing-check",
            pattern_id="epic-qa:missing-check:pattern-a",
            check_key="pattern-a",
            created_at=now,
            occurrence="a-2",
        ),
        _lesson_memory(
            memory_id="pattern-a-third",
            source_kind="task_validation",
            lesson_type="missing-check",
            pattern_id="epic-qa:missing-check:pattern-a",
            check_key="pattern-a",
            created_at=now - timedelta(hours=1),
            occurrence="a-3",
        ),
        _lesson_memory(
            memory_id="b-pattern",
            source_kind="task_validation",
            lesson_type="missing-check",
            pattern_id="epic-qa:missing-check:pattern-b",
            check_key="pattern-b",
            created_at=now - timedelta(hours=2),
            occurrence="b-1",
        ),
        _lesson_memory(
            memory_id="b-pattern-old",
            source_kind="task_validation",
            lesson_type="missing-check",
            pattern_id="epic-qa:missing-check:pattern-b",
            check_key="pattern-b",
            created_at=now - timedelta(days=2),
            occurrence="b-2",
        ),
        _lesson_memory(
            memory_id="c-pattern",
            source_kind="task_validation",
            lesson_type="missing-check",
            pattern_id="epic-qa:missing-check:pattern-c",
            check_key="pattern-c",
            created_at=now - timedelta(hours=2),
            occurrence="c-1",
        ),
        _lesson_memory(
            memory_id="c-pattern-old",
            source_kind="task_validation",
            lesson_type="missing-check",
            pattern_id="epic-qa:missing-check:pattern-c",
            check_key="pattern-c",
            created_at=now - timedelta(days=3),
            occurrence="c-2",
        ),
    ]
    expected_ids = ["a-pattern-a-new", "b-pattern", "c-pattern"]

    for shuffled in (memories, list(reversed(memories)), memories[3:] + memories[:3]):
        memory_manager = FakeMemoryManager()
        memory_manager.memories = shuffled
        result = await _service(memory_manager).recall_review_lessons_by_class(
            lesson_domain="code",
            lesson_types=["missing-check"],
            limit=3,
        )

        assert [lesson["memory_id"] for lesson in result["lessons"]] == expected_ids
        assert [lesson["occurrence_count"] for lesson in result["lessons"]] == [3, 2, 2]

    memory_manager = FakeMemoryManager()
    memory_manager.memories = memories
    clamped = await _service(memory_manager).recall_review_lessons_by_class(
        lesson_domain="code",
        lesson_types=["missing-check"],
        limit=0,
    )
    assert [lesson["memory_id"] for lesson in clamped["lessons"]] == expected_ids[:1]


@pytest.mark.asyncio
async def test_list_check_keys_completeness() -> None:
    now = datetime(2026, 7, 23, tzinfo=UTC)
    expected = [f"long-check-key-{index:03d}-with-stable-identity" for index in range(105)]
    memories = [
        _lesson_memory(
            memory_id=f"lesson-{index:03d}",
            source_kind="plan_review",
            lesson_type="missing-section",
            pattern_id=f"plan-review:missing-section:correctness:{check_key}",
            check_key=check_key,
            category="correctness",
            created_at=now + timedelta(seconds=index),
            occurrence=f"key-{index:03d}",
        )
        for index, check_key in enumerate(reversed(expected))
    ]
    memories.extend(
        [
            memories[0],
            _lesson_memory(
                memory_id="other-project",
                source_kind="plan_review",
                lesson_type="missing-section",
                pattern_id="plan-review:missing-section:correctness:other-project-key",
                check_key="other-project-key",
                category="correctness",
                created_at=now,
                occurrence="other-project",
                project_id="other-project",
            ),
            _lesson_memory(
                memory_id="global",
                source_kind="plan_review",
                lesson_type="missing-section",
                pattern_id="plan-review:missing-section:correctness:global-key",
                check_key="global-key",
                category="correctness",
                created_at=now,
                occurrence="global",
                project_id=None,
            ),
            _lesson_memory(
                memory_id="other-category",
                source_kind="plan_review",
                lesson_type="missing-section",
                pattern_id="plan-review:missing-section:coverage:coverage-key",
                check_key="coverage-key",
                category="coverage",
                created_at=now,
                occurrence="coverage",
            ),
        ]
    )
    assert any(
        tag.startswith("pattern:") and "long-check-key" not in tag for tag in memories[0].tags or []
    )
    memory_manager = FakeMemoryManager()
    memory_manager.memories = memories

    result = await _service(memory_manager).list_check_keys(
        lesson_domain="plan",
        lesson_type="missing-section",
        category="correctness",
    )

    assert result == {"count": len(expected), "check_keys": expected}


@pytest.mark.asyncio
async def test_empty_class_lessons_do_not_consume_limit() -> None:
    now = datetime(2026, 7, 23, tzinfo=UTC)
    empty = _lesson_memory(
        memory_id="empty-newest",
        source_kind="task_validation",
        lesson_type="missing-check",
        pattern_id="epic-qa:missing-check:empty",
        check_key="empty",
        created_at=now,
        occurrence="empty",
        principle=" ",
        prevention="\t",
    )
    actionable = _lesson_memory(
        memory_id="actionable-older",
        source_kind="task_validation",
        lesson_type="missing-check",
        pattern_id="epic-qa:missing-check:actionable",
        check_key="actionable",
        created_at=now - timedelta(days=1),
        occurrence="actionable",
    )
    memory_manager = FakeMemoryManager()
    memory_manager.memories = [empty, actionable]

    result = await _service(memory_manager).recall_review_lessons_by_class(
        lesson_domain="code",
        lesson_types=["missing-check"],
        limit=1,
    )

    assert result["count"] == 1
    assert [lesson["memory_id"] for lesson in result["lessons"]] == ["actionable-older"]
    assert "empty-newest" not in result["message"]
