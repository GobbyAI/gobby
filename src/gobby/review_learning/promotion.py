"""Promotion ladder for repeated review lessons."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

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
    skipped_reason: str | None = None
    missing_guardrail_fields: tuple[str, ...] = ()


class PromotionMemoryManager(Protocol):
    def list_memories(
        self,
        *,
        project_id: str,
        memory_type: str,
        limit: int,
        offset: int = 0,
        tags_all: list[str],
    ) -> list[Any]: ...

    async def alist_memories(
        self,
        *,
        project_id: str,
        memory_type: str,
        limit: int,
        offset: int = 0,
        tags_all: list[str],
    ) -> list[Any]: ...


class PromotionTaskManager(Protocol):
    def list_tasks(
        self,
        *,
        project_id: str,
        closed: bool,
        label: str,
        limit: int,
    ) -> list[Any]: ...

    def update_task(self, task_id: str, **kwargs: Any) -> Any: ...

    def create_task(
        self,
        project_id: str,
        title: str,
        description: str | None = ...,
        *,
        created_in_session_id: str | None = ...,
        priority: int = ...,
        task_type: str = ...,
        labels: list[str] | None = ...,
        category: str | None = ...,
        validation_criteria: str | None = ...,
        implementation_domain: str | None = ...,
        **kwargs: Any,
    ) -> Any: ...


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
        missing_fields = _missing_guardrail_fields(lesson)
        if missing_fields:
            return PromotionDecision(
                tier,
                occurrence_count,
                None,
                None,
                False,
                "insufficient_guardrail_signal",
                missing_fields,
            )
        return PromotionDecision(tier, occurrence_count, target, TARGET_CATEGORY[target], True)

    if lesson.decision == "no-fix-policy":
        if occurrence_count < 2:
            return PromotionDecision("policy-lesson", occurrence_count, None, None, False)
        target = (
            lesson.guardrail_target
            if lesson.guardrail_target in {"checklist", "tool-config"}
            else "checklist"
        )
        missing_fields = _missing_guardrail_fields(lesson)
        if missing_fields:
            return PromotionDecision(
                "policy-guardrail",
                occurrence_count,
                None,
                None,
                False,
                "insufficient_guardrail_signal",
                missing_fields,
            )
        return PromotionDecision(
            "policy-guardrail",
            occurrence_count,
            target,
            TARGET_CATEGORY[target],
            True,
        )

    return PromotionDecision("skipped", occurrence_count, None, None, False)


async def promote_lesson(
    *,
    lesson: NormalizedLesson,
    evidence_memory_id: str,
    memory_manager: PromotionMemoryManager,
    task_manager: PromotionTaskManager,
    project_id: str,
    source_session_id: str | None,
) -> dict[str, Any]:
    """Create or update a guardrail implementation task when thresholds cross."""
    occurrence_memories = await memory_manager.alist_memories(
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
    if decision.skipped_reason is not None:
        result["skipped_reason"] = decision.skipped_reason
        result["missing_guardrail_fields"] = list(decision.missing_guardrail_fields)
    if not decision.should_create_task or decision.guardrail_target is None:
        return result

    task = await asyncio.to_thread(
        _create_or_update_task,
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
    explicit_target = lesson.guardrail_target
    if occurrence_count < 2:
        if lesson.risk != "high":
            return None
        if explicit_target is not None and explicit_target in {
            "rule",
            "workflow",
            "pipeline",
            "validation",
        }:
            return explicit_target
        return "test"
    if occurrence_count == 2:
        if explicit_target is not None and explicit_target in {
            "helper",
            "test",
            "checklist",
            "rule",
            "workflow",
            "pipeline",
        }:
            return explicit_target
        return "test"
    if explicit_target is not None and explicit_target in {
        "helper",
        "test",
        "checklist",
        "rule",
        "workflow",
        "pipeline",
    }:
        return explicit_target
    return "validation"


def _missing_guardrail_fields(lesson: NormalizedLesson) -> tuple[str, ...]:
    missing: list[str] = []
    if not _has_guardrail_value(lesson.finding.get("prevention")):
        missing.append("prevention")
    if not (
        _has_guardrail_value(lesson.finding.get("principle"))
        or _has_guardrail_value(lesson.finding.get("root_cause"))
    ):
        missing.append("principle_or_root_cause")
    if not _has_implementation_anchor(lesson):
        missing.append("implementation_anchor")
    return tuple(missing)


def _has_implementation_anchor(lesson: NormalizedLesson) -> bool:
    finding_anchor_fields = (
        "path",
        "symbol",
        "rule_id",
        "rule_url",
        "query_hints",
        "suggestion",
    )
    evidence_anchor_fields = ("files", "changed_files")
    return any(
        _has_guardrail_value(lesson.finding.get(field)) for field in finding_anchor_fields
    ) or any(_has_guardrail_value(lesson.evidence.get(field)) for field in evidence_anchor_fields)


def _has_guardrail_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_guardrail_value(item) for item in value.values())
    if isinstance(value, list | tuple | set):
        return any(_has_guardrail_value(item) for item in value)
    return bool(value)


def _create_or_update_task(
    *,
    lesson: NormalizedLesson,
    evidence_memory_id: str,
    evidence_memories: list[Any],
    decision: PromotionDecision,
    task_manager: PromotionTaskManager,
    project_id: str,
    source_session_id: str | None,
) -> Any:
    # resolve_promotion normally supplies both fields before this helper is called.
    if decision.guardrail_target is None:
        raise ValueError("promotion decision requires guardrail_target")
    if decision.category is None:
        raise ValueError("promotion decision requires category")
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


def _find_existing_task(
    task_manager: PromotionTaskManager,
    project_id: str,
    pattern_key: str,
) -> Any | None:
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
            "Build or update a guardrail for a review-learning pattern.",
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
