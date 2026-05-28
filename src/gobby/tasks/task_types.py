"""Task type constants and normalization helpers."""

from __future__ import annotations

# Valid task types exposed across storage, CLI, HTTP, and MCP creation surfaces.
TASK_TYPE_CHOICES: tuple[str, ...] = (
    "task",
    "bug",
    "feature",
    "epic",
    "chore",
    "refactor",
    "simple_fix",
    "research_spike",
    "architecture_doc",
    "prd_doc",
    "review_anchor",
)
VALID_TASK_TYPES: frozenset[str] = frozenset(TASK_TYPE_CHOICES)
TASK_TYPE_ALIASES: dict[str, str] = {
    "documentation": "chore",
    "docs": "chore",
    "fix": "simple_fix",
    "nit": "simple_fix",
    "performance": "task",
    "research": "research_spike",
    "test": "task",
}


def validate_task_type(task_type: str | None) -> str:
    """Validate and normalize a task type value."""
    if task_type is None:
        raise ValueError("task_type is required")
    if not isinstance(task_type, str):
        raise ValueError("task_type must be a string")
    raw = task_type.lower().strip()
    normalized = TASK_TYPE_ALIASES.get(raw, raw)
    if normalized not in VALID_TASK_TYPES:
        allowed = ", ".join(TASK_TYPE_CHOICES)
        raise ValueError(f"Invalid task_type '{task_type}'. Expected one of: {allowed}.")
    return normalized


def task_type_filter_values(task_type: str) -> tuple[str, ...]:
    """Return canonical and legacy storage values for a task type filter."""
    canonical = validate_task_type(task_type)
    aliases = tuple(alias for alias, target in TASK_TYPE_ALIASES.items() if target == canonical)
    return (canonical, *aliases)
