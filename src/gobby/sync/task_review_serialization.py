"""The independent-review portion of a task backup."""

from typing import Literal, TypedDict, cast

from gobby.storage.tasks import Task

ReviewStatus = Literal["pending", "valid", "invalid", "error"]


class RestoredTaskReview(TypedDict):
    review_status: ReviewStatus
    review_feedback: str | None
    review_fail_count: int
    review_epoch: int
    validation_criteria: str | None
    validation_override_reason: str | None


def serialize_task_review(task: Task) -> dict[str, object]:
    """Preserve the current review epoch and criteria in the backup contract."""
    return {
        "status": task.review_status or "pending",
        "feedback": task.review_feedback,
        "fail_count": task.review_fail_count,
        "epoch": task.review_epoch,
        "criteria": task.validation_criteria,
        "override_reason": task.validation_override_reason,
    }


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
