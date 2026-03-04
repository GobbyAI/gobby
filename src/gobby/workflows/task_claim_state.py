"""Helpers for managing multi-task claim state in session variables.

Sessions can claim N tasks simultaneously. The state is a single dict
``claimed_tasks: {uuid: "#N"}`` mapping task UUIDs to display refs.
``task_claimed`` is True when the dict is non-empty.
"""

from typing import Any


def add_claimed_task(variables: dict[str, Any], task_id: str, ref: str) -> dict[str, Any]:
    """Return merge dict that adds a task to the claimed set (idempotent)."""
    tasks = dict(variables.get("claimed_tasks") or {})
    tasks[task_id] = ref
    return {"task_claimed": True, "claimed_tasks": tasks}


def remove_claimed_task(variables: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Return merge dict that removes a task from the claimed set."""
    tasks = dict(variables.get("claimed_tasks") or {})
    tasks.pop(task_id, None)
    return {"task_claimed": len(tasks) > 0, "claimed_tasks": tasks}
