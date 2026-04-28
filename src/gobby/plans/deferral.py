"""Deferred plan section validation against task storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from gobby.plans._artifact_refs import artifact_referenced
from gobby.plans.parser import Deferral

type DeferralStatus = Literal[
    "valid",
    "task_missing",
    "task_closed",
    "missing_provenance_label",
    "validation_criteria_does_not_duplicate",
    "missing_reason_or_owner",
    "missing_dependency_or_cited_parent",
]

_OPEN_STATUSES = frozenset({"open", "in_progress", "needs_review", "review_approved", "escalated"})


@dataclass(frozen=True)
class DeferralValidationResult:
    deferral: Deferral
    section_id: str
    plan_id: str
    status: DeferralStatus
    detail: str


class TaskStoreProtocol(Protocol):
    def get_task(self, task_ref: str) -> dict[str, Any] | None: ...
    def get_task_labels(self, task_ref: str) -> list[str]: ...
    def get_task_dependencies(self, task_ref: str) -> list[str]: ...


def validate_deferral(
    deferral: Deferral,
    plan_id: str,
    section_id: str,
    task_store: TaskStoreProtocol,
    *,
    recovery_epic_ref: str,
) -> DeferralValidationResult:
    task = task_store.get_task(deferral.task_ref)
    if task is None:
        return _result(deferral, section_id, plan_id, "task_missing", "task is missing")

    status = _task_status(task)
    if status not in _OPEN_STATUSES:
        return _result(
            deferral,
            section_id,
            plan_id,
            "task_closed",
            f"task has non-open status {status!r}",
        )

    labels = task_store.get_task_labels(deferral.task_ref)
    provenance_label = f"deferred-from:{plan_id}:{section_id}"
    if provenance_label not in labels:
        return _result(
            deferral,
            section_id,
            plan_id,
            "missing_provenance_label",
            f"task labels do not include {provenance_label!r}",
        )

    validation_criteria = _task_validation_criteria(task)
    for item in deferral.original_acceptance_items:
        if not artifact_referenced(item, validation_criteria):
            return _result(
                deferral,
                section_id,
                plan_id,
                "validation_criteria_does_not_duplicate",
                f"task validation criteria do not duplicate artifact {item.artifact_ref!r}",
            )

    if not deferral.reason.strip() or not deferral.owner.strip():
        return _result(
            deferral,
            section_id,
            plan_id,
            "missing_reason_or_owner",
            "deferral reason and owner are required",
        )

    dependency_closure = _dependency_closure(recovery_epic_ref, task_store)
    if deferral.task_ref in dependency_closure or _has_valid_cited_parent(
        labels=labels,
        dependency_closure=dependency_closure,
        recovery_epic_ref=recovery_epic_ref,
        task_store=task_store,
    ):
        return _result(deferral, section_id, plan_id, "valid", "deferral is valid")

    return _result(
        deferral,
        section_id,
        plan_id,
        "missing_dependency_or_cited_parent",
        "task is neither a recovery epic dependency nor a valid cited-parent deferral",
    )


def _result(
    deferral: Deferral,
    section_id: str,
    plan_id: str,
    status: DeferralStatus,
    detail: str,
) -> DeferralValidationResult:
    return DeferralValidationResult(
        deferral=deferral,
        section_id=section_id,
        plan_id=plan_id,
        status=status,
        detail=detail,
    )


def _task_status(task: dict[str, Any]) -> str:
    return str(task.get("status", "")).strip()


def _task_validation_criteria(task: dict[str, Any]) -> str:
    value = task.get("validation_criteria", "")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _dependency_closure(root_ref: str, task_store: TaskStoreProtocol) -> set[str]:
    seen: set[str] = set()
    stack = list(task_store.get_task_dependencies(root_ref))

    while stack:
        task_ref = stack.pop()
        if task_ref in seen:
            continue
        seen.add(task_ref)
        stack.extend(task_store.get_task_dependencies(task_ref))

    return seen


def _has_valid_cited_parent(
    *,
    labels: list[str],
    dependency_closure: set[str],
    recovery_epic_ref: str,
    task_store: TaskStoreProtocol,
) -> bool:
    out_of_scope_label = f"out-of-scope-for:{recovery_epic_ref}"

    for label in labels:
        if not label.startswith("cited-parent:"):
            continue
        parent_ref = label.removeprefix("cited-parent:")
        if not parent_ref or parent_ref in dependency_closure:
            continue
        parent_task = task_store.get_task(parent_ref)
        if parent_task is None or _task_status(parent_task) not in _OPEN_STATUSES:
            continue
        if out_of_scope_label in task_store.get_task_labels(parent_ref):
            return True

    return False


__all__ = [
    "DeferralValidationResult",
    "TaskStoreProtocol",
    "validate_deferral",
]
