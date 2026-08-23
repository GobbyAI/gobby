"""Helpers for managing multi-task claim state in session variables.

Sessions can claim N tasks simultaneously. The state is a single dict
``claimed_tasks: {uuid: "#N"}`` mapping task UUIDs to display refs.
``task_claimed`` is True when the dict is non-empty.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any


def normalize_task_edited_path(value: object) -> str | None:
    """Normalize one repository-relative task attribution path."""
    if not isinstance(value, str) or not value:
        return None
    path = posixpath.normpath(value.replace("\\", "/"))
    if path in {"", "."} or path.startswith("../") or path.startswith("/"):
        return None
    return path


def normalize_task_checkout_root(value: object) -> str | None:
    """Normalize one absolute checkout root used by task edit attribution."""
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return None
    return str(path.resolve(strict=False))


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


def _task_edited_file_checkouts(
    variables: dict[str, Any],
) -> dict[str, dict[str, list[str]]]:
    raw = variables.get("task_edited_file_checkouts") or {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, list[str]]] = {}
    for task_id, raw_checkouts in raw.items():
        if not isinstance(raw_checkouts, dict):
            continue
        checkouts: dict[str, list[str]] = {}
        for raw_root, raw_files in raw_checkouts.items():
            root = normalize_task_checkout_root(raw_root)
            if root is None or not isinstance(raw_files, list):
                continue
            checkouts[root] = sorted(
                {
                    path
                    for value in raw_files
                    if (path := normalize_task_edited_path(value)) is not None
                }
            )
        result[str(task_id)] = checkouts
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


def release_claimed_task(variables: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Return merge dict that releases a claim and keeps the task's edit attribution.

    Escalation and claim handoff pause a task rather than finish it, so the files it
    edited are still its own. Keeping `task_edited_files` preserves what the close
    checklist reads for gates 7, 9, 10 and 12, and what `commit_guard` reads to name
    the owner of a dirty path in a shared worktree.
    """
    tasks = _claimed_tasks(variables)
    tasks.pop(task_id, None)

    active_task_id = variables.get("active_task_id")
    if active_task_id == task_id or active_task_id not in tasks:
        active_task_id = _active_task_id_after_removal(tasks)

    result: dict[str, Any] = {
        "task_claimed": len(tasks) > 0,
        "claimed_tasks": tasks,
        "active_task_id": active_task_id,
    }
    if not tasks:
        result["task_has_commits"] = False
    return result


def remove_claimed_task(variables: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Return merge dict that removes a finished task and its edit attribution."""
    task_files = _task_edited_files(variables)
    task_files.pop(task_id, None)
    task_file_checkouts = _task_edited_file_checkouts(variables)
    task_file_checkouts.pop(task_id, None)

    result = release_claimed_task(variables, task_id)
    result["task_edited_files"] = task_files
    if "task_edited_file_checkouts" in variables:
        result["task_edited_file_checkouts"] = task_file_checkouts
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


def task_edited_file_set_for_checkout(
    variables: dict[str, Any],
    task_id: str | None,
    checkout_root: str,
) -> set[str]:
    """Return task-attributed paths recorded in one absolute checkout root."""
    root = normalize_task_checkout_root(checkout_root)
    if not task_id or root is None:
        return set()
    return set(_task_edited_file_checkouts(variables).get(task_id, {}).get(root, []))


def target_task_has_edits(variables: dict[str, Any], task_id: str | None) -> bool:
    """Return whether the resolved task has attributed edit paths."""
    return bool(task_id and _task_edited_files(variables).get(task_id))
