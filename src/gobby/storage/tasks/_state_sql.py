"""SQL helpers for canonical task lifecycle predicates."""

from __future__ import annotations

from typing import Any


def _col(alias: str | None, column: str) -> str:
    if alias:
        return f"{alias}.{column}"
    return column


def canonical_status_case(alias: str | None = None) -> str:
    """Return the SQL CASE expression for projected legacy status."""
    closed_at = _col(alias, "closed_at")
    escalated_at = _col(alias, "escalated_at")
    lifecycle_stage = _col(alias, "lifecycle_stage")
    return (
        f"CASE "
        f"WHEN {closed_at} IS NOT NULL THEN 'closed' "
        f"WHEN {escalated_at} IS NOT NULL THEN 'escalated' "
        f"WHEN {lifecycle_stage} IS NOT NULL THEN {lifecycle_stage} "
        "ELSE 'open' END"
    )


def is_closed_sql(alias: str | None = None) -> str:
    """Return the SQL predicate for canonical closed state."""
    return f"{_col(alias, 'closed_at')} IS NOT NULL"


def is_unresolved_sql(alias: str | None = None) -> str:
    """Return the SQL predicate for tasks that are not canonically closed."""
    return f"{_col(alias, 'closed_at')} IS NULL"


def is_ready_sql(alias: str | None = None) -> str:
    """Return the SQL predicate for tasks that can still execute work."""
    closed_at = _col(alias, "closed_at")
    escalated_at = _col(alias, "escalated_at")
    lifecycle_stage = _col(alias, "lifecycle_stage")
    return (
        f"{closed_at} IS NULL "
        f"AND {escalated_at} IS NULL "
        f"AND ({lifecycle_stage} IS NULL OR {lifecycle_stage} = 'in_progress')"
    )


def is_merge_ready_sql(alias: str | None = None) -> str:
    """Return the SQL predicate for tasks that passed review."""
    closed_at = _col(alias, "closed_at")
    escalated_at = _col(alias, "escalated_at")
    lifecycle_stage = _col(alias, "lifecycle_stage")
    return (
        f"{closed_at} IS NULL "
        f"AND {escalated_at} IS NULL "
        f"AND {lifecycle_stage} = 'review_approved'"
    )


def status_filter_sql(
    status: str | list[str] | None,
    *,
    alias: str | None = None,
) -> tuple[str | None, list[Any]]:
    """Build a canonical status filter clause and params."""
    if not status:
        return None, []

    statuses = [status] if isinstance(status, str) else list(status)
    projected_status = canonical_status_case(alias)
    placeholders = ", ".join("?" for _ in statuses)
    return f"{projected_status} IN ({placeholders})", statuses
