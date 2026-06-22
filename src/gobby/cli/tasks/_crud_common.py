"""Shared helpers for task CRUD CLI commands."""

from typing import Any

import click

from gobby.storage.tasks import TASK_TYPE_CHOICES

TASK_TYPE_CHOICE = click.Choice(TASK_TYPE_CHOICES)
ISOLATION_CHOICE = click.Choice(["none", "worktree", "clone"])


def current_stage_display(state: dict[str, Any]) -> str:
    current_stage = state.get("current_stage")
    if isinstance(current_stage, dict):
        name = current_stage.get("name")
        stage_state = current_stage.get("state")
        if name and stage_state:
            return f"{name}:{stage_state}"
        if stage_state:
            return str(stage_state)
    return "ready"
