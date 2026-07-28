"""Task validation-criteria invariants and deterministic criterion splitting."""

from __future__ import annotations

import re

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>.+?)\s*$")


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


def split_validation_criteria(value: str | None) -> tuple[str, ...]:
    """Split free-text criteria into stable, distinct criterion strings."""
    normalized = normalized_validation_criteria(value)
    if normalized is None:
        return ()

    lines = normalized.splitlines()
    items: list[str] = []
    current: list[str] = []
    saw_list_marker = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if current:
                items.append(" ".join(current))
                current = []
            continue
        match = _LIST_ITEM_RE.match(raw_line)
        if match is not None:
            if not saw_list_marker:
                items = []
                current = []
            saw_list_marker = True
            if current:
                items.append(" ".join(current))
            current = [match.group("text").strip()]
            continue
        current.append(line)

    if current:
        items.append(" ".join(current))

    if saw_list_marker:
        return tuple(item for item in items if item)

    paragraphs = tuple(item for item in items if item)
    return paragraphs or (normalized,)
