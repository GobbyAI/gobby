"""Ordinary task criteria text normalization and creation requirements."""

from __future__ import annotations


class TaskCriteriaError(ValueError):
    """Raised when a non-epic task has no observable validation contract."""


def normalized_validation_criteria(value: str | None) -> str | None:
    """Return stripped criteria, treating whitespace-only values as absent."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def require_validation_criteria(task_type: str, value: str | None) -> str | None:
    """Enforce the criteria invariant and return the normalized value."""
    normalized = normalized_validation_criteria(value)
    if task_type != "epic" and normalized is None:
        raise TaskCriteriaError(
            "Every non-epic task requires nonempty validation_criteria. "
            "State observable completion evidence before creating or updating the task."
        )
    return normalized
