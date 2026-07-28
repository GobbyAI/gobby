from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from gobby.review_learning.promotion import PromotionTaskManager
from gobby.review_learning.service import ReviewLearningMemoryManager, ReviewLearningService
from tests.review_learning.conftest import (
    PROJECT_SCOPE_ID,
    FakeMemory,
    FakeMemoryManager,
    FakeTaskManager,
)

pytestmark = pytest.mark.unit


def _service(
    memory_manager: FakeMemoryManager,
    task_manager: FakeTaskManager,
) -> ReviewLearningService:
    return ReviewLearningService(
        cast(ReviewLearningMemoryManager, memory_manager),
        cast(PromotionTaskManager, task_manager),
    )


def _class_finding(
    *,
    namespace: str,
    lesson_type: str,
    check_key: str,
    guardrail_target: str,
) -> dict[str, str]:
    if namespace == "plan-review":
        category = "correctness"
        pattern_id = f"{namespace}:{lesson_type}:{category}:{check_key}"
        anchor = {"category": category, "rule_id": f"plan-review:{category}"}
    else:
        pattern_id = f"{namespace}:{lesson_type}:{check_key}"
        anchor = {
            "path": "src/gobby/review_learning/service.py",
            "symbol": "ReviewLearningService.record",
        }
    return {
        "title": f"Repeated {lesson_type} finding",
        "lesson_type": lesson_type,
        "pattern_id": pattern_id,
        "finding_fingerprint": f"{namespace}:{lesson_type}:{check_key}",
        "check_key": check_key,
        "principle": "Equivalent failures share stable class-scoped identity.",
        "prevention": f"Add the {lesson_type} guardrail before the next review.",
        "guardrail_target": guardrail_target,
        **anchor,
    }


async def _record_pair(
    service: ReviewLearningService,
    *,
    source_kind: str,
    source: str,
    finding: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    first = await service.record(
        source_kind=source_kind,
        source=source,
        source_review=f"{source}:round-1",
        decision="confirmed",
        finding=finding,
        evidence={"proof": "first occurrence"},
    )
    second = await service.record(
        source_kind=source_kind,
        source=source,
        source_review=f"{source}:round-2",
        decision="confirmed",
        finding=finding,
        evidence={"proof": "second occurrence"},
    )
    return first, second


@pytest.mark.asyncio
async def test_per_class_promotion() -> None:
    memory_manager = FakeMemoryManager()
    task_manager = FakeTaskManager()
    service = _service(memory_manager, task_manager)
    class_cases = [
        ("plan-review", "plan_review", "plan-adversary-reviewer", "reviewer-miss", "checklist"),
        (
            "plan-review",
            "plan_review",
            "plan-adversary-fixer",
            "fixer-induced-defect",
            "checklist",
        ),
        ("epic-qa", "qa_rejection", "epic-qa-reviewer", "qa-miss", "checklist"),
        (
            "epic-qa",
            "qa_rejection",
            "epic-validation-reviewer",
            "validation-miss",
            "validation",
        ),
    ]
    promotion_results: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    for namespace, source_kind, source, lesson_type, target in class_cases:
        finding = _class_finding(
            namespace=namespace,
            lesson_type=lesson_type,
            check_key=f"{lesson_type}-contract",
            guardrail_target=target,
        )
        finding_snapshot = deepcopy(finding)
        promotion_results[lesson_type] = await _record_pair(
            service,
            source_kind=source_kind,
            source=source,
            finding=finding,
        )
        assert finding == finding_snapshot

    validation_finding = {
        "title": "Validation receipt loses its command provenance",
        "lesson_type": "recurring-validation-failure",
        "pattern_id": ("task-validation:recurring-validation-failure:receipt-command-provenance"),
        "finding_fingerprint": "task-validation:receipt-command-provenance",
        "check_key": "receipt-command-provenance",
        "root_cause": "Receipt correlation discarded the initiating command.",
        "prevention": "Preserve the command through every terminal wait boundary.",
        "path": "src/gobby/tasks/validation.py",
        "symbol": "TaskValidator.validate",
        "guardrail_target": "validation",
    }
    first_candidate = deepcopy(validation_finding)
    second_candidate = deepcopy(validation_finding)
    first_validation = await service.record(
        source_kind="task_validation",
        source="task-validation",
        source_review="task-validation:task-a",
        decision="confirmed",
        finding=first_candidate,
        evidence={"failed_iteration": "task-a", "passing_close": "receipt-a"},
    )
    second_validation = await service.record(
        source_kind="task_validation",
        source="task-validation",
        source_review="task-validation:task-b",
        decision="confirmed",
        finding=second_candidate,
        evidence={"failed_iteration": "task-b", "passing_close": "receipt-b"},
    )
    promotion_results["recurring-validation-failure"] = (
        first_validation,
        second_validation,
    )

    assert first_validation["pattern_id"] == second_validation["pattern_id"]
    assert first_validation["finding_fingerprint"] == second_validation["finding_fingerprint"]
    assert first_validation["occurrence_key"] != second_validation["occurrence_key"]

    expected_targets = {
        "reviewer-miss": "checklist",
        "fixer-induced-defect": "checklist",
        "qa-miss": "checklist",
        "validation-miss": "validation",
        "recurring-validation-failure": "validation",
    }
    assert set(promotion_results) == set(expected_targets)
    for lesson_type, (first, second) in promotion_results.items():
        assert first["occurrence_count"] == 1, lesson_type
        assert first["guardrail_target"] is None, lesson_type
        assert second["occurrence_count"] == 2, lesson_type
        assert second["guardrail_target"] == expected_targets[lesson_type], lesson_type
        assert second["task_ref"], lesson_type

    assert len(task_manager.created) == len(expected_targets)
    assert {label for task in task_manager.tasks for label in task.labels} >= {
        "target:checklist",
        "target:validation",
    }


@pytest.mark.asyncio
async def test_cross_domain_coexistence() -> None:
    colliding_path = "src/gobby/review_learning/service.py"
    memory_manager = FakeMemoryManager()
    task_manager = FakeTaskManager()
    service = _service(memory_manager, task_manager)
    memory_manager.memories.append(
        FakeMemory(
            id="legacy-ordinary",
            content="Gobby psycopg storage uses %s placeholders.",
            memory_type="fact",
            project_id=PROJECT_SCOPE_ID,
            tags=["sql"],
        )
    )
    code_result = await service.record(
        source_kind="agent_review",
        source="code-reviewer",
        source_review="code-review:1",
        decision="confirmed",
        finding={
            "title": "Keep the legacy code lesson",
            "lesson_type": "legacy-code",
            "pattern_id": "legacy-code-path-contract",
            "principle": "Code lessons remain visible to code recall.",
            "prevention": "Recall the code lesson for its matching file.",
            "path": colliding_path,
        },
        evidence={"proof": "legacy behavior"},
    )
    recall_args: dict[str, Any] = {
        "findings": [{"title": "Review the service", "query_hints": ["review learning"]}],
        "source": "coderabbit",
        "source_kind": "review_comment",
    }
    legacy_before = await service.recall_context(**recall_args)

    plan_result = await service.record(
        source_kind="plan_review",
        source="plan-adversary",
        source_review="plan-review:1",
        decision="confirmed",
        finding={
            "title": "Plan lesson with a colliding code path",
            "lesson_type": "reviewer-miss",
            "pattern_id": "plan-review:reviewer-miss:correctness:domain-partition",
            "finding_fingerprint": "plan-review:reviewer-miss:domain-partition",
            "check_key": "domain-partition",
            "category": "correctness",
            "principle": "Plan lessons stay inside plan recall.",
            "prevention": "Filter plan lessons by domain tags.",
            "path": colliding_path,
        },
        evidence={"proof": "plan-only lesson"},
    )
    legacy_after = await service.recall_context(**recall_args)
    file_recall = await service.recall_review_lessons_for_files(
        file_paths=[colliding_path],
        project_id=PROJECT_SCOPE_ID,
        limit=10,
    )

    assert legacy_before == legacy_after
    assert plan_result["lesson_id"] not in {match["memory_id"] for match in legacy_after["matches"]}
    assert {lesson["memory_id"] for lesson in file_recall["lessons"]} == {code_result["lesson_id"]}


@pytest.mark.asyncio
async def test_check_key_convergence() -> None:
    memory_manager = FakeMemoryManager()
    task_manager = FakeTaskManager()
    service = _service(memory_manager, task_manager)
    canonical_key = "canonical-state-check"
    competing_keys = [f"competing-check-{index}" for index in range(6)]
    all_keys = [canonical_key, *competing_keys]
    now = datetime(2026, 7, 23, tzinfo=UTC)

    for index, check_key in enumerate(all_keys):
        result = await service.record(
            source_kind="plan_review",
            source="plan-adversary",
            source_review=f"catalog:{check_key}",
            decision="confirmed",
            finding=_class_finding(
                namespace="plan-review",
                lesson_type="reviewer-miss",
                check_key=check_key,
                guardrail_target="checklist",
            ),
            evidence={"proof": f"catalog entry {index}"},
        )
        memory = next(item for item in memory_manager.memories if item.id == result["lesson_id"])
        memory.created_at = now + timedelta(minutes=index)

    capped_recall = await service.recall_review_lessons_by_class(
        lesson_domain="plan",
        lesson_types=["reviewer-miss"],
        source_kinds=["plan_review"],
        limit=5,
    )
    recalled_patterns = {lesson["pattern_id"] for lesson in capped_recall["lessons"]}
    canonical_pattern = f"plan-review:reviewer-miss:correctness:{canonical_key}"
    assert canonical_pattern not in recalled_patterns

    catalog = await service.list_check_keys(
        lesson_domain="plan",
        lesson_type="reviewer-miss",
        category="correctness",
    )
    assert catalog == {"count": len(all_keys), "check_keys": sorted(all_keys)}
    assert canonical_key in catalog["check_keys"]

    equivalent_a = _class_finding(
        namespace="plan-review",
        lesson_type="reviewer-miss",
        check_key=canonical_key,
        guardrail_target="checklist",
    )
    equivalent_a["title"] = "State ownership is ambiguous"
    equivalent_b = dict(equivalent_a)
    equivalent_b["title"] = "The state has two apparent owners"
    second = await service.record(
        source_kind="plan_review",
        source="plan-adversary",
        source_review="equivalent:round-2",
        decision="confirmed",
        finding=equivalent_a,
        evidence={"proof": "equivalent phrasing a"},
    )
    third = await service.record(
        source_kind="plan_review",
        source="plan-adversary",
        source_review="equivalent:round-3",
        decision="confirmed",
        finding=equivalent_b,
        evidence={"proof": "equivalent phrasing b"},
    )

    assert second["pattern_id"] == third["pattern_id"] == canonical_pattern
    assert second["finding_fingerprint"] == third["finding_fingerprint"]
    assert second["occurrence_count"] == 2
    assert third["occurrence_count"] == 3
    assert second["task_ref"] == third["task_ref"]
    assert len(task_manager.tasks) == 1
