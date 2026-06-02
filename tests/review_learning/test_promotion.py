from __future__ import annotations

import pytest

from gobby.review_learning.service import ReviewLearningService

pytestmark = pytest.mark.unit


def _finding(**overrides: str) -> dict[str, str]:
    finding = {
        "title": "Durable writes missing",
        "pattern_id": "durable-write-after-state-change",
        "lesson_type": "durable-writes",
        "principle": "Persist state after changing it",
        "root_cause": "Mutation happened without a storage write",
        "prevention": "Add regression coverage around persistence",
    }
    finding.update(overrides)
    return finding


@pytest.mark.asyncio
async def test_confirmed_second_occurrence_creates_test_guardrail_task(
    fake_memory_manager,
    fake_task_manager,
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    first = await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(),
        evidence={"commit": "abc"},
    )
    second = await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-2",
        decision="confirmed",
        finding=_finding(),
        evidence={"commit": "def"},
    )

    assert first["guardrail_target"] is None
    assert second["guardrail_target"] == "test"
    assert second["task_ref"] == "#1"
    assert fake_task_manager.created[0]["category"] == "test"
    assert "review-learning" in fake_task_manager.tasks[0].labels
    assert "guardrail" in fake_task_manager.tasks[0].labels
    assert "mem-1" in fake_task_manager.tasks[0].description
    assert "mem-2" in fake_task_manager.tasks[0].description


@pytest.mark.asyncio
async def test_third_confirmed_occurrence_updates_existing_task(
    fake_memory_manager, fake_task_manager
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)
    for source_review in ("review-1", "review-2", "review-3"):
        result = await service.record(
            source_kind="agent_review",
            source="code-reviewer",
            source_review=source_review,
            decision="confirmed",
            finding=_finding(),
            evidence={"commit": source_review},
        )

    assert result["guardrail_target"] == "validation"
    assert len(fake_task_manager.created) == 1
    assert len(fake_task_manager.updated) == 1
    assert "target:validation" in fake_task_manager.tasks[0].labels
    assert fake_task_manager.tasks[0].category == "code"


@pytest.mark.asyncio
async def test_duplicate_occurrence_preflight_skips_new_memory(
    fake_memory_manager, fake_task_manager
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(),
        evidence={"commit": "abc"},
    )
    duplicate = await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(),
        evidence={"commit": "abc"},
    )

    assert duplicate["skipped_reason"] == "duplicate_occurrence"
    assert len(fake_memory_manager.memories) == 1


@pytest.mark.asyncio
async def test_no_fix_policy_only_promotes_to_checklist(
    fake_memory_manager, fake_task_manager
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)
    for source_review in ("review-1", "review-2"):
        result = await service.record(
            source_kind="review_comment",
            source="coderabbit",
            source_review=source_review,
            decision="no-fix-policy",
            finding=_finding(guardrail_target="rule"),
            evidence={"reason": "tool profile is intentionally noisy"},
        )

    assert result["guardrail_target"] == "checklist"
    assert fake_task_manager.created[0]["category"] == "docs"


@pytest.mark.asyncio
async def test_non_promotable_lessons_never_create_tasks(
    fake_memory_manager, fake_task_manager
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    for source_review in ("review-1", "review-2"):
        result = await service.record(
            source_kind="review_comment",
            source="coderabbit",
            source_review=source_review,
            decision="confirmed",
            finding={"title": "One-off comment"},
            evidence={"commit": source_review},
        )

    assert result["promotable"] is False
    assert result["guardrail_target"] is None
    assert fake_task_manager.created == []
