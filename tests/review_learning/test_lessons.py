from __future__ import annotations

from typing import cast

import pytest

from gobby.review_learning.file_paths import path_tag
from gobby.review_learning.fingerprint import build_occurrence_key
from gobby.review_learning.lessons import (
    CI_SOURCE_KINDS,
    SOURCE_KIND_DOMAIN,
    VALID_SOURCE_KINDS,
    derive_lesson_domain,
    derive_lesson_identity,
    has_verified_fix,
    normalize_lesson,
    validate_check_key,
    validate_guardrail_target,
    validate_source_kind,
)
from gobby.review_learning.promotion import PromotionTaskManager
from gobby.review_learning.service import ReviewLearningMemoryManager, ReviewLearningService
from tests.review_learning.conftest import FakeMemoryManager, FakeTaskManager

pytestmark = pytest.mark.unit


def _service(
    memory_manager: FakeMemoryManager,
    task_manager: FakeTaskManager,
) -> ReviewLearningService:
    return ReviewLearningService(
        cast(ReviewLearningMemoryManager, memory_manager),
        cast(PromotionTaskManager, task_manager),
    )


def test_tool_config_is_a_valid_guardrail_target() -> None:
    assert validate_guardrail_target("tool-config") == "tool-config"


def test_source_kind_domain_map_total() -> None:
    assert SOURCE_KIND_DOMAIN.keys() == VALID_SOURCE_KINDS
    assert {derive_lesson_domain(source_kind) for source_kind in VALID_SOURCE_KINDS} == {
        "code",
        "plan",
    }
    assert derive_lesson_domain("plan_review") == "plan"
    assert derive_lesson_domain("task_validation") == "code"
    assert validate_source_kind("plan_review") == "plan_review"
    assert validate_source_kind("task_validation") == "task_validation"
    assert {"plan_review", "task_validation"}.isdisjoint(CI_SOURCE_KINDS)

    with pytest.raises(ValueError, match="Unmapped lesson source kind"):
        derive_lesson_domain("unmapped")


@pytest.mark.parametrize("check_key", ["durable-write", "sql-1", "a"])
def test_validate_check_key_accepts_kebab_case(check_key: str) -> None:
    assert validate_check_key(check_key) == check_key


@pytest.mark.parametrize("check_key", ["", "Durable-write", "durable write", "durable_write"])
def test_validate_check_key_rejects_non_kebab_case(check_key: str) -> None:
    with pytest.raises(ValueError, match="Invalid check key"):
        validate_check_key(check_key)


def test_pattern_id_derives_from_lesson_type_and_principle() -> None:
    identity = derive_lesson_identity(
        {"lesson_type": "sql-placeholders", "principle": "Use psycopg %s placeholders"}
    )

    assert identity.promotable is True
    assert identity.pattern_id.startswith("sql-placeholders:")
    assert identity.pattern_key.startswith("sql-placeholders")


def test_non_promotable_fallback_when_pattern_is_underivable() -> None:
    identity = derive_lesson_identity({"title": "One-off finding"})

    assert identity.promotable is False
    assert identity.pattern_id.startswith("non-promotable:")


def test_non_promotable_fallback_is_independent_of_finding_key_order() -> None:
    first = derive_lesson_identity({"risk": "medium", "decision": "confirmed"})
    reordered = derive_lesson_identity({"decision": "confirmed", "risk": "medium"})

    assert first == reordered


@pytest.mark.asyncio
async def test_domain_and_check_key_tags(
    fake_memory_manager: FakeMemoryManager,
    fake_task_manager: FakeTaskManager,
) -> None:
    service = _service(fake_memory_manager, fake_task_manager)
    result = await service.record(
        source_kind="plan_review",
        source="plan-adversary",
        source_review="plan-round-1",
        decision="confirmed",
        finding={
            "title": "A stale section survived review",
            "pattern_id": "plan-review:reviewer-miss:correctness-safety:stale-section",
            "lesson_type": "reviewer-miss",
            "principle": "Review every changed section",
            "check_key": "stale-section",
            "category": "Correctness & Safety",
            "finding_fingerprint": "plan-round-1:reviewer-miss",
        },
        evidence={"participating_section_ids": ["p1"]},
    )

    memory = next(
        memory for memory in fake_memory_manager.memories if memory.id == result["lesson_id"]
    )
    assert "lesson-domain:plan" in (memory.tags or [])
    assert "check-key:stale-section" in (memory.tags or [])
    assert "category:correctness-safety" in (memory.tags or [])
    assert memory.created_by_agent == "review-learning"
    assert memory.rationale == (
        "Confirmed review finding (stale-section): recurring pattern worth "
        "re-serving when similar code is reviewed"
    )

    code_lesson = normalize_lesson(
        source_kind="review_comment",
        source="coderabbit",
        source_review="review-1",
        decision="confirmed",
        finding={"title": "Code finding", "pattern_id": "code-finding"},
        evidence={},
        finding_fingerprint="code-fingerprint",
        occurrence_key=build_occurrence_key("review-1", "code-fingerprint"),
        repo=None,
        language=None,
        risk="medium",
    )
    assert "lesson-domain:code" in code_lesson.tags
    assert not any(tag.startswith(("check-key:", "category:")) for tag in code_lesson.tags)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("namespace", "source_kind", "lesson_types"),
    [
        ("plan-review", "plan_review", ("reviewer-miss", "fixer-induced-defect")),
        ("epic-qa", "qa_rejection", ("qa-miss", "validation-miss")),
    ],
)
async def test_dual_class_identity_separation(
    namespace: str,
    source_kind: str,
    lesson_types: tuple[str, str],
    fake_memory_manager: FakeMemoryManager,
    fake_task_manager: FakeTaskManager,
) -> None:
    service = _service(fake_memory_manager, fake_task_manager)
    check_key = "stale-section"

    def finding(lesson_type: str) -> dict[str, str]:
        category = "correctness"
        if namespace == "plan-review":
            pattern_id = f"{namespace}:{lesson_type}:{category}:{check_key}"
        else:
            pattern_id = f"{namespace}:{lesson_type}:{check_key}"
        result = {
            "title": "The same finding supports two lesson classes",
            "principle": "Use class-scoped lesson identity",
            "pattern_id": pattern_id,
            "lesson_type": lesson_type,
            "check_key": check_key,
            "finding_fingerprint": f"shared-finding:{lesson_type}",
        }
        if namespace == "plan-review":
            result["category"] = category
        return result

    first = await service.record(
        source_kind=source_kind,
        source="multi-class-recorder",
        source_review="review-1",
        decision="confirmed",
        finding=finding(lesson_types[0]),
        evidence={"proof": "first class"},
    )
    second = await service.record(
        source_kind=source_kind,
        source="multi-class-recorder",
        source_review="review-1",
        decision="confirmed",
        finding=finding(lesson_types[1]),
        evidence={"proof": "second class"},
    )
    repeated_first = await service.record(
        source_kind=source_kind,
        source="multi-class-recorder",
        source_review="review-2",
        decision="confirmed",
        finding=finding(lesson_types[0]),
        evidence={"proof": "first class repeated"},
    )

    assert first["pattern_id"] != second["pattern_id"]
    assert first["finding_fingerprint"] != second["finding_fingerprint"]
    assert first["occurrence_key"] != second["occurrence_key"]
    assert first["occurrence_count"] == second["occurrence_count"] == 1
    assert repeated_first["occurrence_count"] == 2
    assert len(fake_memory_manager.memories) == 3


def test_namespaced_identity_requires_explicit_valid_check_key() -> None:
    with pytest.raises(ValueError, match="check_key"):
        derive_lesson_identity(
            {
                "pattern_id": "epic-qa:qa-miss:stale-section",
                "lesson_type": "qa-miss",
            }
        )

    with pytest.raises(ValueError, match="Invalid check key"):
        derive_lesson_identity(
            {
                "pattern_id": "plan-review:reviewer-miss:correctness:Stale-Section",
                "lesson_type": "reviewer-miss",
                "category": "correctness",
                "check_key": "Stale-Section",
            }
        )


def test_normalized_lesson_uses_bounded_tags_and_full_content() -> None:
    finding = {
        "title": "Wrong placeholder",
        "pattern_id": "Use psycopg %s placeholders in Gobby storage code",
        "lesson_type": "sql-placeholders",
        "rule_id": "SQL001",
        "severity": "high",
        "path": "src/gobby/storage/example.py",
        "start_line": 4,
        "query_hints": ["psycopg", "%s"],
    }
    occurrence_key = build_occurrence_key("review-1", "native-1")

    lesson = normalize_lesson(
        source_kind="review_comment",
        source="coderabbit",
        source_review="review-1",
        decision="confirmed",
        finding=finding,
        evidence={"commit": "abc123"},
        finding_fingerprint="native-1",
        occurrence_key=occurrence_key,
        repo="josh/gobby",
        language="python",
        risk="high",
    )

    assert "review-lesson" in lesson.tags
    assert "confirmed" in lesson.tags
    assert "source-kind:review_comment" in lesson.tags
    assert "source:coderabbit" in lesson.tags
    assert any(tag.startswith("fingerprint:") for tag in lesson.tags)
    assert any(tag.startswith("occurrence:") for tag in lesson.tags)
    assert not any(tag.startswith("guardrail:") for tag in lesson.tags)
    assert path_tag("src/gobby/storage/example.py") in lesson.tags
    assert "Use psycopg %s placeholders in Gobby storage code" in lesson.content
    assert '"commit": "abc123"' in lesson.content


def test_normalized_lesson_tags_evidence_paths() -> None:
    lesson = normalize_lesson(
        source_kind="review_comment",
        source="coderabbit",
        source_review="review-1",
        decision="confirmed",
        finding={
            "title": "Evidence path only",
            "pattern_id": "evidence-path-only",
            "principle": "Use evidence paths for lookup",
        },
        evidence={"changed_files": ["src/gobby/review_learning/service.py"]},
        finding_fingerprint="native-1",
        occurrence_key=build_occurrence_key("review-1", "native-1"),
        repo="josh/gobby",
        language="python",
        risk="medium",
    )

    assert path_tag("src/gobby/review_learning/service.py") in lesson.tags


@pytest.mark.parametrize(
    "evidence",
    [
        {"commit": "abc"},
        {"commit_sha": "abc"},
        {"verified_fix_ref": "abc"},
        {"fix_ref": "abc"},
        {"changes_id": "abc"},
    ],
)
def test_verified_fix_detection(evidence: dict[str, str]) -> None:
    assert has_verified_fix(evidence)
