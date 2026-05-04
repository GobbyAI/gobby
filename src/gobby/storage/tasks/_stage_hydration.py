"""Helpers for hydrating task stage rows onto Task objects."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._stage_types import StageState


def _coerce_artifact_refs(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        return None
    return {str(key): str(item) for key, item in decoded.items()}


def _state_from_row(row: Any) -> StageState:
    return StageState(
        task_id=row["task_id"],
        stage_name=row["stage_name"],
        position=int(row["position"]),
        state=row["state"],
        review_policy=row["review_policy"],
        reviewer_agent=row["reviewer_agent"],
        entered_at=row["entered_at"],
        entered_by_session_id=row["entered_by_session_id"],
        completed_at=row["completed_at"],
        completed_by_session_id=row["completed_by_session_id"],
        completed_commit_sha=row["completed_commit_sha"],
        work_attempt_count=int(row["work_attempt_count"]),
        review_round_count=int(row["review_round_count"]),
        max_work_attempts=row["max_work_attempts"],
        max_review_rounds=row["max_review_rounds"],
        artifact_refs=_coerce_artifact_refs(row["artifact_refs"]),
        notes=row["notes"],
        updated_at=row["updated_at"],
    )


def hydrate_task_stage_state(db: DatabaseProtocol, tasks: Sequence[Task]) -> None:
    """Populate denormalized stage rows on task objects."""
    if not tasks:
        return

    task_ids = [task.id for task in tasks]
    placeholders = ", ".join("?" for _ in task_ids)
    rows = db.fetchall(
        f"""
        SELECT *
          FROM task_stage_states
         WHERE task_id IN ({placeholders})
         ORDER BY task_id, position, stage_name
        """,  # nosec B608 - placeholders are generated from task_ids length only.
        tuple(task_ids),
    )

    grouped: dict[str, list[StageState]] = {task_id: [] for task_id in task_ids}
    for row in rows:
        grouped.setdefault(row["task_id"], []).append(_state_from_row(row))

    for task in tasks:
        task.stages = tuple(grouped.get(task.id, ()))
