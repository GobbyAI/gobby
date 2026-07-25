from __future__ import annotations

import asyncio

import pytest

import gobby.review_learning.service as service_module
from gobby.review_learning.lessons import normalize_lesson
from gobby.review_learning.promotion import (
    PromotionDecision,
    PromotionMemoryManager,
    _create_or_update_task,
    _diagnostic_locations,
    resolve_promotion,
)
from gobby.review_learning.service import ReviewLearningService
from tests.review_learning.conftest import FakeTaskManager

pytestmark = pytest.mark.unit


def _finding(**overrides: str) -> dict[str, str]:
    finding = {
        "title": "Durable writes missing",
        "pattern_id": "durable-write-after-state-change",
        "lesson_type": "durable-writes",
        "principle": "Persist state after changing it",
        "root_cause": "Mutation happened without a storage write",
        "prevention": "Add regression coverage around persistence",
        "path": "src/gobby/tasks/state.py",
    }
    finding.update(overrides)
    return finding


def test_promotion_memory_protocol_only_exposes_used_listing_method() -> None:
    assert "list_memories" not in PromotionMemoryManager.__dict__
    assert "alist_memories" in PromotionMemoryManager.__dict__


def test_diagnostic_location_ignores_end_line_without_start_line() -> None:
    assert _diagnostic_locations({"path": "a.py", "end_line": 5}) == "- a.py"


@pytest.mark.asyncio
async def test_high_risk_weak_lesson_records_memory_without_task(
    fake_memory_manager,
    fake_task_manager,
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    result = await service.record(
        source_kind="test_failure",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(prevention="", path=""),
        evidence={"commit": "abc"},
        risk="high",
    )

    assert result["guardrail_target"] is None
    assert result["skipped_reason"] == "insufficient_guardrail_signal"
    assert set(result["missing_guardrail_fields"]) == {"prevention", "implementation_anchor"}
    assert len(fake_memory_manager.memories) == 1
    assert fake_task_manager.created == []


@pytest.mark.asyncio
async def test_guardrail_signal_ignores_dict_keys(
    fake_memory_manager,
    fake_task_manager,
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    result = await service.record(
        source_kind="test_failure",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding={
            "title": "Guardrail signal from dictionary keys",
            "pattern_id": "dict-key-guardrail-signal",
            "lesson_type": "review-signal",
            "principle": {"non_empty_key": ""},
            "root_cause": {},
            "prevention": {"another_key": "  "},
            "path": {"src/gobby/example.py": ""},
        },
        evidence={"commit": "abc", "changed_files": {"src/gobby/example.py": ""}},
        risk="high",
    )

    assert result["guardrail_target"] is None
    assert result["skipped_reason"] == "insufficient_guardrail_signal"
    assert set(result["missing_guardrail_fields"]) == {
        "prevention",
        "principle_or_root_cause",
        "implementation_anchor",
    }
    assert len(fake_memory_manager.memories) == 1
    assert fake_task_manager.created == []


@pytest.mark.asyncio
async def test_high_risk_actionable_first_occurrence_creates_test_by_default(
    fake_memory_manager,
    fake_task_manager,
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    result = await service.record(
        source_kind="test_failure",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(),
        evidence={"commit": "abc"},
        risk="high",
    )

    assert result["guardrail_target"] == "test"
    assert result["task_ref"] == "#1"
    assert fake_task_manager.created[0]["category"] == "test"
    assert "target:test" in fake_task_manager.tasks[0].labels
    assert "(1x, target=test)" in fake_task_manager.tasks[0].title
    assert "repeated review-learning pattern" not in fake_task_manager.tasks[0].description


@pytest.mark.asyncio
async def test_high_risk_reviewer_sourced_first_occurrence_stays_lesson_only(
    fake_memory_manager,
    fake_task_manager,
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    result = await service.record(
        source_kind="agent_review",
        source="plan-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(),
        evidence={"commit": "abc"},
        risk="high",
    )

    assert result["tier"] == "lesson"
    assert result["guardrail_target"] is None
    assert "task_ref" not in result
    assert len(fake_memory_manager.memories) == 1
    assert fake_task_manager.created == []


@pytest.mark.asyncio
async def test_high_risk_actionable_explicit_rule_target_creates_rule_task(
    fake_memory_manager,
    fake_task_manager,
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    result = await service.record(
        source_kind="test_failure",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(guardrail_target="rule"),
        evidence={"commit": "abc"},
        risk="high",
    )

    assert result["guardrail_target"] == "rule"
    assert result["task_ref"] == "#1"
    assert fake_task_manager.created[0]["category"] == "config"
    assert "target:rule" in fake_task_manager.tasks[0].labels


@pytest.mark.parametrize("target", ["validation", "tool-config"])
@pytest.mark.asyncio
async def test_high_risk_actionable_preserves_explicit_valid_targets(
    fake_memory_manager,
    fake_task_manager,
    target: str,
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    result = await service.record(
        source_kind="test_failure",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(guardrail_target=target),
        evidence={"commit": "abc"},
        risk="high",
    )

    assert result["guardrail_target"] == target
    assert result["task_ref"] == "#1"
    assert f"target:{target}" in fake_task_manager.tasks[0].labels


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
    assert "review-learning pattern" in fake_task_manager.tasks[0].description
    assert "repeated review-learning pattern" not in fake_task_manager.tasks[0].description


@pytest.mark.parametrize(
    "occurrence_count, expected_tier",
    [
        pytest.param(2, "confirmed-2", id="second-occurrence"),
        pytest.param(3, "confirmed-3", id="third-occurrence"),
        pytest.param(5, "confirmed-3", id="later-occurrence"),
    ],
)
def test_confirmed_thresholds_preserve_explicit_tool_config_target(
    occurrence_count: int,
    expected_tier: str,
) -> None:
    lesson = normalize_lesson(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(guardrail_target="tool-config"),
        evidence={"commit": "abc"},
        finding_fingerprint="fingerprint",
        occurrence_key="occurrence",
        repo=None,
        language=None,
        risk="medium",
    )

    decision = resolve_promotion(lesson, occurrence_count)

    assert decision.tier == expected_tier
    assert decision.guardrail_target == "tool-config"
    assert decision.category == "config"
    assert decision.should_create_task is True


@pytest.mark.asyncio
async def test_repeated_weak_confirmed_lessons_wait_for_actionable_occurrence(
    fake_memory_manager,
    fake_task_manager,
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    weak_finding = _finding(prevention="", path="")
    first = await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=weak_finding,
        evidence={"commit": "abc"},
    )
    second = await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-2",
        decision="confirmed",
        finding=weak_finding,
        evidence={"commit": "def"},
    )
    third = await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-3",
        decision="confirmed",
        finding=_finding(),
        evidence={"commit": "ghi"},
    )

    assert first["guardrail_target"] is None
    assert "skipped_reason" not in first
    assert second["guardrail_target"] is None
    assert second["skipped_reason"] == "insufficient_guardrail_signal"
    assert third["guardrail_target"] == "validation"
    assert third["task_ref"] == "#1"
    assert len(fake_memory_manager.memories) == 3
    assert len(fake_task_manager.created) == 1


@pytest.mark.asyncio
async def test_third_confirmed_occurrence_updates_existing_task(
    fake_memory_manager, fake_task_manager
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)
    for source, source_review in (
        ("Reviewer One", "Review One"),
        ("Reviewer Two", "Review Two"),
        ("Reviewer Three", "Review Three"),
    ):
        result = await service.record(
            source_kind="agent_review",
            source=source,
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
    assert [
        label for label in fake_task_manager.tasks[0].labels if label.startswith("evidence:")
    ] == ["evidence:mem-3"]
    assert [
        label for label in fake_task_manager.tasks[0].labels if label.startswith("source:")
    ] == ["source:reviewer-three"]
    assert [
        label for label in fake_task_manager.tasks[0].labels if label.startswith("review-lesson:")
    ] == ["review-lesson:review-three"]


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
async def test_duplicate_occurrence_retries_promotion_after_partial_failure(
    fake_memory_manager,
    fake_task_manager,
    monkeypatch: pytest.MonkeyPatch,
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

    real_promote_lesson = service_module.promote_lesson

    async def fail_promotion(**_: object) -> dict[str, object]:
        raise RuntimeError("promotion unavailable")

    monkeypatch.setattr(service_module, "promote_lesson", fail_promotion)
    with pytest.raises(RuntimeError, match="promotion unavailable"):
        await service.record(
            source_kind="agent_review",
            source="code-reviewer",
            source_review="review-2",
            decision="confirmed",
            finding=_finding(),
            evidence={"commit": "def"},
        )

    monkeypatch.setattr(service_module, "promote_lesson", real_promote_lesson)
    retry = await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-2",
        decision="confirmed",
        finding=_finding(),
        evidence={"commit": "def"},
    )

    assert retry["skipped_reason"] == "duplicate_occurrence"
    assert retry["guardrail_target"] == "test"
    assert retry["task_ref"] == "#1"
    assert len(fake_memory_manager.memories) == 2
    assert len(fake_task_manager.created) == 1


@pytest.mark.asyncio
async def test_distinct_findings_with_formerly_colliding_identity_are_both_recorded(
    fake_memory_manager,
    fake_task_manager,
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    rule_result = await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(rule_id="X", principle=""),
        evidence={"commit": "abc"},
    )
    principle_result = await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(rule_id="", principle="X"),
        evidence={"commit": "abc"},
    )

    assert rule_result["finding_fingerprint"] != principle_result["finding_fingerprint"]
    assert rule_result["occurrence_key"] != principle_result["occurrence_key"]
    assert principle_result.get("skipped_reason") != "duplicate_occurrence"
    assert len(fake_memory_manager.memories) == 2


@pytest.mark.asyncio
async def test_concurrent_same_occurrence_creates_one_memory_and_guardrail_task(
    fake_memory_manager,
    fake_task_manager,
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

    results = await asyncio.gather(
        *(
            service.record(
                source_kind="agent_review",
                source="code-reviewer",
                source_review="review-2",
                decision="confirmed",
                finding=_finding(),
                evidence={"commit": "def"},
            )
            for _ in range(2)
        )
    )

    assert len(fake_memory_manager.memories) == 2
    assert len(fake_task_manager.tasks) == 1
    assert sum(result.get("skipped_reason") == "duplicate_occurrence" for result in results) == 1
    assert sum(result.get("task_ref") == "#1" for result in results) == 2


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


async def test_no_fix_policy_promotion_preserves_confirmed_guardrail_task(
    fake_memory_manager, fake_task_manager
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)
    for index in range(1, 4):
        await service.record(
            source_kind="agent_review",
            source="code-reviewer",
            source_review=f"confirmed-{index}",
            decision="confirmed",
            finding=_finding(),
            evidence={"commit": f"confirmed-{index}"},
        )

    confirmed_task = fake_task_manager.tasks[0]
    for index in range(1, 4):
        await service.record(
            source_kind="review_comment",
            source="coderabbit",
            source_review=f"policy-{index}",
            decision="no-fix-policy",
            finding=_finding(),
            evidence={"reason": f"policy-{index}"},
        )

    assert len(fake_task_manager.created) == 2
    assert confirmed_task.category == "code"
    assert "target:validation" in confirmed_task.labels
    assert "decision:confirmed" in confirmed_task.labels
    policy_task = fake_task_manager.tasks[1]
    assert policy_task.category == "docs"
    assert "target:checklist" in policy_task.labels
    assert "decision:no-fix-policy" in policy_task.labels
    assert "implementation_domain" not in fake_task_manager.created[1]
    assert "implementation_domain" not in fake_task_manager.updated[-1]


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
            finding={
                "title": "One-off comment",
                "prevention": "Keep this one-off guidance in memory only.",
            },
            evidence={"commit": source_review},
        )

    assert result["promotable"] is False
    assert result["guardrail_target"] is None
    assert fake_task_manager.created == []


def test_create_or_update_task_rejects_incomplete_promotion_decision(
    fake_task_manager: FakeTaskManager,
) -> None:
    lesson = normalize_lesson(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="review-1",
        decision="confirmed",
        finding=_finding(),
        evidence={"commit": "abc"},
        finding_fingerprint="fingerprint",
        occurrence_key="occurrence",
        repo=None,
        language=None,
        risk="medium",
    )

    with pytest.raises(ValueError, match="guardrail_target"):
        _create_or_update_task(
            lesson=lesson,
            evidence_memory_id="mem-1",
            evidence_memories=[],
            decision=PromotionDecision("broken", 2, None, "test", True),
            task_manager=fake_task_manager,
            project_id="project",
            source_session_id=None,
        )

    with pytest.raises(ValueError, match="category"):
        _create_or_update_task(
            lesson=lesson,
            evidence_memory_id="mem-1",
            evidence_memories=[],
            decision=PromotionDecision("broken", 2, "test", None, True),
            task_manager=fake_task_manager,
            project_id="project",
            source_session_id=None,
        )
