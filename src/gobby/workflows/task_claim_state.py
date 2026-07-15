"""Helpers for managing multi-task claim state in session variables.

Sessions can claim N tasks simultaneously. The state is a single dict
``claimed_tasks: {uuid: "#N"}`` mapping task UUIDs to display refs.
``task_claimed`` is True when the dict is non-empty.
"""

from __future__ import annotations

from typing import Any


def _claimed_tasks(variables: dict[str, Any]) -> dict[str, str]:
    raw = variables.get("claimed_tasks") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(task_id): str(ref) for task_id, ref in raw.items()}


def _task_edited_files(variables: dict[str, Any]) -> dict[str, list[str]]:
    raw = variables.get("task_edited_files") or {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for task_id, files in raw.items():
        if isinstance(files, list):
            result[str(task_id)] = sorted({str(file) for file in files if file})
    return result


def _active_task_id_after_removal(tasks: dict[str, str]) -> str | None:
    if len(tasks) == 1:
        return next(iter(tasks))
    return None


def add_claimed_task(variables: dict[str, Any], task_id: str, ref: str) -> dict[str, Any]:
    """Return merge dict that adds a task to the claimed set (idempotent)."""
    tasks = _claimed_tasks(variables)
    tasks[task_id] = ref
    return {"task_claimed": True, "claimed_tasks": tasks, "active_task_id": task_id}


def remove_claimed_task(variables: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Return merge dict that removes a task from the claimed set."""
    tasks = _claimed_tasks(variables)
    tasks.pop(task_id, None)
    task_files = _task_edited_files(variables)
    task_files.pop(task_id, None)

    active_task_id = variables.get("active_task_id")
    if active_task_id == task_id or active_task_id not in tasks:
        active_task_id = _active_task_id_after_removal(tasks)

    result = {
        "task_claimed": len(tasks) > 0,
        "claimed_tasks": tasks,
        "active_task_id": active_task_id,
        "task_edited_files": task_files,
    }
    if not tasks:
        result["task_has_commits"] = False
    return result


def active_task_id_for_edit(variables: dict[str, Any]) -> str | None:
    """Return the task that should receive a new edited-file attribution."""
    tasks = _claimed_tasks(variables)
    if not tasks:
        return None

    active_task_id = variables.get("active_task_id")
    if isinstance(active_task_id, str) and active_task_id in tasks:
        return active_task_id
    if len(tasks) == 1:
        return next(iter(tasks))
    return None


def resolve_target_task_id(variables: dict[str, Any], task_ref: Any) -> str | None:
    """Resolve a lifecycle tool task reference to a task UUID tracked in session variables."""
    if task_ref is None:
        return None
    raw_ref = str(task_ref)
    if not raw_ref:
        return None

    claimed = _claimed_tasks(variables)
    task_files = _task_edited_files(variables)
    if raw_ref in claimed or raw_ref in task_files:
        return raw_ref

    aliases = {raw_ref}
    if raw_ref.isdigit():
        aliases.add(f"#{raw_ref}")
    for task_id, display_ref in claimed.items():
        if str(display_ref) in aliases:
            return task_id
    return None


def task_edited_file_set(variables: dict[str, Any], task_id: str | None) -> set[str]:
    """Return repo-relative files attributed to a task."""
    if not task_id:
        return set()
    return set(_task_edited_files(variables).get(task_id, []))


def target_task_has_edits(variables: dict[str, Any], task_id: str | None) -> bool:
    """Return whether the resolved target task has any attributed edits."""
    return bool(task_edited_file_set(variables, task_id))
