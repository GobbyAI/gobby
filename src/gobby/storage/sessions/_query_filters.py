"""SQL predicate construction for session queries."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

TASK_REF_ROLE_COLUMNS: dict[str, str] = {
    "claimed": "claimed_by_session_id",
    "created": "created_in_session_id",
    "closed": "closed_in_session_id",
}


def build_session_filters(
    project_id: str | None,
    status: str | None,
    source: str | None,
    *,
    machine_id: str | None = None,
    sources: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    modes: Sequence[str] | None = None,
    models: Sequence[str] | None = None,
    session_seq_min: int | None = None,
    session_seq_max: int | None = None,
    task_ref_min: int | None = None,
    task_ref_max: int | None = None,
    task_ref_roles: Sequence[str] | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    agent_type: str | None = None,
) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if agent_type:
        # The seat definition a session was typed with, stored as its _agent_type variable.
        conditions.append(
            "id IN (SELECT session_id FROM session_variables "
            "WHERE variables ->> '_agent_type' = %s)"
        )
        params.append(agent_type)

    if status == "deleted":
        conditions.append("status = 'deleted'")
    else:
        conditions.append("status != 'deleted'")
        if status:
            conditions.append("status = %s")
            params.append(status)
        if statuses:
            placeholders = ",".join(["%s"] * len(statuses))
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)

    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    if source:
        conditions.append("source = %s")
        params.append(source)
    if machine_id:
        conditions.append("machine_id = %s")
        params.append(machine_id)
    if sources:
        placeholders = ",".join(["%s"] * len(sources))
        conditions.append(f"source IN ({placeholders})")
        params.extend(sources)

    # Mode resolves to agent_depth: interactive = depth 0, auto = depth >= 1.
    # Empty / both = no filter (the user wants everything).
    if modes:
        unique_modes = set(modes)
        if "interactive" in unique_modes and "auto" not in unique_modes:
            conditions.append("agent_depth = 0")
        elif "auto" in unique_modes and "interactive" not in unique_modes:
            conditions.append("agent_depth >= 1")
        # "both selected" or "neither selected" → no filter

    if models:
        placeholders = ",".join(["%s"] * len(models))
        conditions.append(f"model IN ({placeholders})")
        params.extend(models)

    if session_seq_min is not None:
        conditions.append("seq_num >= %s")
        params.append(session_seq_min)
    if session_seq_max is not None:
        conditions.append("seq_num <= %s")
        params.append(session_seq_max)

    # Task-ref overlap: a session matches if any task with seq_num in
    # [min, max] is linked to it via any of the selected roles. Default role
    # is "claimed" — the most useful axis ("which sessions worked on tasks
    # in range X").
    if task_ref_min is not None or task_ref_max is not None:
        roles = list(task_ref_roles) if task_ref_roles else ["claimed"]
        role_clauses: list[str] = []
        for role in roles:
            col = TASK_REF_ROLE_COLUMNS.get(role)
            if col is None:
                continue
            bounds: list[str] = []
            if task_ref_min is not None:
                bounds.append("seq_num >= %s")
                params.append(task_ref_min)
            if task_ref_max is not None:
                bounds.append("seq_num <= %s")
                params.append(task_ref_max)
            bound_sql = " AND ".join(bounds)
            role_clauses.append(
                f"EXISTS (SELECT 1 FROM tasks WHERE {col} = sessions.id AND {bound_sql})"  # nosec B608
            )
        if role_clauses:
            conditions.append(f"({' OR '.join(role_clauses)})")

    if created_after:
        conditions.append("created_at >= %s")
        params.append(created_after)
    if created_before:
        conditions.append("created_at < %s")
        params.append(created_before)

    return conditions, params
