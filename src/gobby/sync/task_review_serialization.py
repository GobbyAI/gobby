"""Task evidence serialization and historical review metadata without close authority."""

from typing import Literal, TypedDict, cast
from uuid import UUID

from gobby.storage.tasks import Task
from gobby.tasks.attestations import TaskAttestation, validate_attestation_history

ReviewStatus = Literal["pending", "valid", "invalid", "error"]


class RestoredTaskReview(TypedDict):
    review_status: ReviewStatus
    review_feedback: str | None
    review_fail_count: int
    review_epoch: int
    validation_criteria: str | None
    validation_override_reason: str | None


def serialize_task_review(task: Task) -> dict[str, object]:
    """Preserve historical summary metadata; it carries no attestation or approval."""
    return {
        "status": task.review_status or "pending",
        "feedback": task.review_feedback,
        "fail_count": task.review_fail_count,
        "epoch": task.review_epoch,
        "criteria": task.validation_criteria,
        "override_reason": task.validation_override_reason,
    }


def deserialize_task_attestations(value: object, *, task_id: str) -> list[TaskAttestation]:
    """Validate the complete generic history before any task-backup writes."""
    entries = [] if value is None else value
    if not isinstance(entries, list):
        raise ValueError("attestations must be a list")
    history: list[TaskAttestation] = []
    for index, entry in enumerate(entries):
        try:
            record = TaskAttestation.model_validate(entry)
            UUID(record.id)
            UUID(record.session_id)
        except ValueError as exc:
            raise ValueError(f"attestations[{index}] is invalid: {exc}") from exc
        history.append(record)
    validate_attestation_history(task_id, history)
    return history


def deserialize_task_review(value: object) -> RestoredTaskReview:
    """Validate before a restore transaction can change any task rows."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("review must be an object")
    if "state" in value:
        raise ValueError("review uses status, not the retired state field")
    status = value.get("status", "pending")
    if status not in ("pending", "valid", "invalid", "error"):
        raise ValueError("review.status must be pending, valid, invalid, or error")
    fail_count = value.get("fail_count", 0)
    epoch = value.get("epoch", 1)
    if type(fail_count) is not int or fail_count < 0:
        raise ValueError("review.fail_count must be a nonnegative integer")
    if type(epoch) is not int or epoch < 1:
        raise ValueError("review.epoch must be a positive integer")
    for field in ("feedback", "criteria", "override_reason"):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise ValueError(f"review.{field} must be a string or null")
    return {
        "review_status": cast(ReviewStatus, status),
        "review_feedback": value.get("feedback"),
        "review_fail_count": fail_count,
        "review_epoch": epoch,
        "validation_criteria": value.get("criteria"),
        "validation_override_reason": value.get("override_reason"),
    }
