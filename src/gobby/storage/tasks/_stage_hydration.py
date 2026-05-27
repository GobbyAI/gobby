"""Helpers for hydrating task stage rows onto Task objects."""

from __future__ import annotations

from collections.abc import Sequence

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._stage_state_rows import state_from_row
from gobby.storage.tasks._stage_types import StageState


def hydrate_task_stage_state(db: HubDatabase, tasks: Sequence[Task]) -> None:
    """Populate denormalized stage rows on task objects."""
    if not tasks:
        return

    task_ids = [task.id for task in tasks]
    placeholders = ", ".join("%s" for _ in task_ids)
    rows = db.fetchall(
        f"""
        SELECT s.*, r.display_label, r.category
          FROM task_stage_states s
          LEFT JOIN task_stages_registry r ON r.name = s.stage_name
         WHERE s.task_id IN ({placeholders})
         ORDER BY s.task_id, s.position, s.stage_name
        """,  # nosec B608 # placeholders are generated from task_ids length only.
        tuple(task_ids),
    )

    grouped: dict[str, list[StageState]] = {task_id: [] for task_id in task_ids}
    for row in rows:
        grouped.setdefault(row["task_id"], []).append(state_from_row(row))

    for task in tasks:
        task.stages = tuple(grouped.get(task.id, ()))
