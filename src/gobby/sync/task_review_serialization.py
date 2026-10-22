"""The independent-review portion of a task backup."""

from collections.abc import Sequence
from typing import Literal, TypedDict, cast
from uuid import UUID

from gobby.storage.tasks import Task
from gobby.tasks.step_records import TaskStepRecord

ReviewStatus = Literal["pending", "valid", "invalid", "error"]


class RestoredTaskReview(TypedDict):
    review_status: ReviewStatus
    review_feedback: str | None
    review_fail_count: int
    review_epoch: int
    validation_criteria: str | None
    validation_override_reason: str | None


def serialize_task_review(task: Task, history: Sequence[TaskStepRecord]) -> dict[str, object]:
    """Preserve the review summary, criteria and the task's step record history."""
    return {
        "status": task.review_status or "pending",
        "feedback": task.review_feedback,
        "fail_count": task.review_fail_count,
        "epoch": task.review_epoch,
        "criteria": task.validation_criteria,
        "override_reason": task.validation_override_reason,
        "step_records": [record.model_dump(mode="json") for record in history],
    }


def deserialize_task_step_records(value: object, *, task_id: str) -> list[TaskStepRecord]:
    """Validate backed-up step history, the only restore input that can carry approval.

    Call after ``deserialize_task_review`` has validated the review object and epoch.
    """
    review = value if isinstance(value, dict) else {}
    entries = review.get("step_records", [])
    if not isinstance(entries, list):
        raise ValueError("review.step_records must be a list")
    epoch = review.get("epoch", 1)
    history: list[TaskStepRecord] = []
    for index, entry in enumerate(entries):
        try:
            record = TaskStepRecord.model_validate(entry)
            UUID(record.id)
            UUID(record.session_id)
        except ValueError as exc:
            raise ValueError(f"review.step_records[{index}] is not a step record: {exc}") from exc
        if record.task_id != task_id:
            raise ValueError(f"review.step_records[{index}] belongs to another task")
        if record.epoch > epoch:
            raise ValueError(f"review.step_records[{index}] is from a later epoch than the task")
        if history and record.sequence <= history[-1].sequence:
            raise ValueError("review.step_records sequences must strictly increase")
        history.append(record)
    if len({record.id for record in history}) != len(history):
        raise ValueError("review.step_records has duplicate record ids")
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
