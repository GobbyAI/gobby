"""Stage-aware task list filter helpers."""

from __future__ import annotations

from collections.abc import Sequence

import click

from gobby.storage.tasks import LocalTaskManager, Task

STAGE_STATES = ("ready", "in_progress", "needs_review", "review_approved", "done")
STAGE_STATE_CHOICE = click.Choice(STAGE_STATES)


def filter_tasks_by_stage(
    manager: LocalTaskManager,
    tasks: Sequence[Task],
    *,
    stage_name: str | None,
    state: str | None,
    project_id: str | None,
) -> list[Task]:
    """Filter an already-loaded task list by exact stage row state."""

    if stage_name is None:
        return list(tasks)

    task_ids = set(
        manager.stage_states.list_tasks_at_stage(
            stage_name=stage_name,
            state=state,
            project_id=project_id,
        )
    )
    return [task for task in tasks if task.id in task_ids]
