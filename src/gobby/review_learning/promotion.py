"""Promotion ladder for repeated review lessons."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.review_learning.lessons import GuardrailTarget, NormalizedLesson

TARGET_CATEGORY: dict[str, str] = {
    "helper": "code",
    "validation": "code",
    "test": "test",
    "rule": "config",
    "workflow": "config",
    "pipeline": "config",
    "tool-config": "config",
    "checklist": "docs",
}


@dataclass(frozen=True)
class PromotionDecision:
    """Deterministic promotion outcome for a pattern."""

    tier: str
    occurrence_count: int
    guardrail_target: GuardrailTarget | None
    category: str | None
    should_create_task: bool


def resolve_promotion(
    lesson: NormalizedLesson,
    occurrence_count: int,
) -> PromotionDecision:
    """Apply the decision-aware promotion ladder."""
    if not lesson.identity.promotable:
        return PromotionDecision("non-promotable", occurrence_count, None, None, False)

    if lesson.decision == "confirmed":
        target = _confirmed_target(lesson, occurrence_count)
        if target is None:
            return PromotionDecision("lesson", occurrence_count, None, None, False)
        tier = "high-risk" if lesson.risk == "high" else f"confirmed-{min(occurrence_count, 3)}"
        return PromotionDecision(tier, occurrence_count, target, TARGET_CATEGORY[target], True)

    if lesson.decision == "no-fix-policy":
        if occurrence_count < 2:
            return PromotionDecision("policy-lesson", occurrence_count, None, None, False)
        target = (
            lesson.guardrail_target
            if lesson.guardrail_target in {"checklist", "tool-config"}
            else "checklist"
        )
        return PromotionDecision(
            "policy-guardrail",
            occurrence_count,
            target,
            TARGET_CATEGORY[target],
            True,
        )

    return PromotionDecision("skipped", occurrence_count, None, None, False)


def promote_lesson(
    *,
    lesson: NormalizedLesson,
    evidence_memory_id: str,
    memory_manager: Any,
    task_manager: Any,
    project_id: str,
    source_session_id: str | None,
) -> dict[str, Any]:
    """Create or update a guardrail implementation task when thresholds cross."""
    occurrence_memories = memory_manager.list_memories(
        project_id=project_id,
        memory_type="pattern",
        limit=500,
        tags_all=["review-lesson", f"pattern:{lesson.identity.pattern_key}", lesson.decision],
    )
    occurrence_count = _count_occurrences(occurrence_memories)
    decision = resolve_promotion(lesson, occurrence_count)
    result: dict[str, Any] = {
        "tier": decision.tier,
        "occurrence_count": decision.occurrence_count,
        "guardrail_target": decision.guardrail_target,
    }
    if not decision.should_create_task or decision.guardrail_target is None:
        return result

    task = _create_or_update_task(
        lesson=lesson,
        evidence_memory_id=evidence_memory_id,
        evidence_memories=occurrence_memories,
        decision=decision,
        task_manager=task_manager,
        project_id=project_id,
        source_session_id=source_session_id,
    )
    result["task_ref"] = _task_ref(task)
    result["task_id"] = getattr(task, "id", None)
    return result


def _confirmed_target(
    lesson: NormalizedLesson,
    occurrence_count: int,
) -> GuardrailTarget | None:
    if lesson.risk == "high":
        if lesson.guardrail_target in {"rule", "workflow", "pipeline", "validation"}:
            return lesson.guardrail_target
        return "rule"
    if occurrence_count < 2:
        return None
    if occurrence_count == 2:
        if lesson.guardrail_target in {"helper", "checklist"}:
            return lesson.guardrail_target
        return "test"
    if lesson.guardrail_target in {"rule", "workflow", "pipeline"}:
        return lesson.guardrail_target
    return "validation"


def _create_or_update_task(
    *,
    lesson: NormalizedLesson,
    evidence_memory_id: str,
    evidence_memories: list[Any],
    decision: PromotionDecision,
    task_manager: Any,
    project_id: str,
    source_session_id: str | None,
) -> Any:
    assert decision.guardrail_target is not None
    assert decision.category is not None
    existing = _find_existing_task(task_manager, project_id, lesson.identity.pattern_key)
    labels = _task_labels(
        lesson=lesson,
        target=decision.guardrail_target,
        evidence_memory_id=evidence_memory_id,
    )
    evidence_ids = _memory_ids(evidence_memories)
    description = _task_description(
        lesson=lesson,
        target=decision.guardrail_target,
        evidence_memory_ids=evidence_ids,
        occurrence_count=decision.occurrence_count,
    )
    validation_criteria = _validation_criteria(lesson, decision.guardrail_target)
    title = (
        f"Guardrail: {lesson.identity.lesson_type} - {lesson.identity.pattern_id} "
        f"({decision.occurrence_count}x, target={decision.guardrail_target})"
    )

    if existing is not None:
        merged_labels = _merge_labels(getattr(existing, "labels", None) or [], labels)
        merged_labels = [label for label in merged_labels if not label.startswith("target:")] + [
            f"target:{decision.guardrail_target}"
        ]
        return task_manager.update_task(
            existing.id,
            title=title,
            description=description,
            labels=merged_labels,
            category=decision.category,
            validation_criteria=validation_criteria,
            implementation_domain="backend" if decision.category == "code" else None,
        )

    return task_manager.create_task(
        project_id=project_id,
        title=title,
        description=description,
        created_in_session_id=source_session_id,
        priority=1 if lesson.risk == "high" else 2,
        task_type="task",
        labels=labels,
        category=decision.category,
        validation_criteria=validation_criteria,
        implementation_domain="backend" if decision.category == "code" else None,
    )


def _find_existing_task(task_manager: Any, project_id: str, pattern_key: str) -> Any | None:
    candidates = task_manager.list_tasks(
        project_id=project_id,
        closed=False,
        label=f"pattern:{pattern_key}",
        limit=50,
    )
    for task in candidates:
        labels = set(getattr(task, "labels", None) or [])
        if {"review-learning", "guardrail"}.issubset(labels):
            return task
    return None


def _task_labels(
    *,
    lesson: NormalizedLesson,
    target: str,
    evidence_memory_id: str,
) -> list[str]:
    labels = [
        "guardrail",
        "review-learning",
        f"pattern:{lesson.identity.pattern_key}",
        f"lesson-type:{lesson.identity.lesson_type}",
        f"target:{target}",
        f"source:{lesson.source}",
        f"review-lesson:{lesson.source_review}",
        f"evidence:{evidence_memory_id}",
    ]
    return _merge_labels([], labels)


def _task_description(
    *,
    lesson: NormalizedLesson,
    target: str,
    evidence_memory_ids: list[str],
    occurrence_count: int,
) -> str:
    evidence_block = "\n".join(f"- {memory_id}" for memory_id in evidence_memory_ids)
    locations = _diagnostic_locations(lesson.finding)
    return "\n".join(
        [
            "Build or update a guardrail for a repeated review-learning pattern.",
            "",
            f"pattern_id: {lesson.identity.pattern_id}",
            f"pattern_key: {lesson.identity.pattern_key}",
            f"decision: {lesson.decision}",
            f"occurrences: {occurrence_count}",
            f"guardrail_target: {target}",
            "",
            "Lesson:",
            f"- principle: {lesson.finding.get('principle', '')}",
            f"- root_cause: {lesson.finding.get('root_cause', '')}",
            f"- prevention: {lesson.finding.get('prevention', '')}",
            "",
            "Diagnostic locations:",
            locations or "- none supplied",
            "",
            "Evidence memory IDs:",
            evidence_block or f"- {lesson.occurrence_key}",
        ]
    )


def _validation_criteria(lesson: NormalizedLesson, target: str) -> str:
    return (
        f"Implement a {target} guardrail for review-learning pattern "
        f"{lesson.identity.pattern_id}. Validation must prove the guardrail catches or "
        "prevents the reusable failure class described in the evidence memories."
    )


def _diagnostic_locations(finding: dict[str, Any]) -> str:
    path = finding.get("path")
    symbol = finding.get("symbol")
    if not path and not symbol:
        return ""
    start = finding.get("start_line")
    end = finding.get("end_line")
    line_suffix = f":{start}" if start else ""
    if end and end != start:
        line_suffix = f"{line_suffix}-{end}"
    symbol_suffix = f" ({symbol})" if symbol else ""
    return f"- {path or '<unknown>'}{line_suffix}{symbol_suffix}"


def _count_occurrences(memories: list[Any]) -> int:
    occurrences: set[str] = set()
    for memory in memories:
        for tag in getattr(memory, "tags", None) or []:
            if isinstance(tag, str) and tag.startswith("occurrence:"):
                occurrences.add(tag)
    return len(occurrences)


def _memory_ids(memories: list[Any]) -> list[str]:
    ids = [str(memory.id) for memory in memories if getattr(memory, "id", None)]
    return sorted(set(ids))


def _merge_labels(existing: list[str], incoming: list[str]) -> list[str]:
    merged: list[str] = []
    for label in [*existing, *incoming]:
        if label not in merged:
            merged.append(label)
    return merged


def _task_ref(task: Any) -> str:
    seq_num = getattr(task, "seq_num", None)
    if seq_num:
        return f"#{seq_num}"
    return str(getattr(task, "id", ""))[:8]
