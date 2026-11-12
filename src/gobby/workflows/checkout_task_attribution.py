"""Resolve registered checkout ownership once, at the tool-start boundary."""

import os
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.workflows.task_claim_state import active_task_id_for_edit, task_selected_at


def checkout_task_selections(
    db: HubDatabase, variables: dict[str, Any], project_id: str, started_at: float
) -> dict[str, str | None]:
    """Freeze owned leaves and selected descendants of shared epic checkouts."""
    claimed = variables.get("claimed_tasks", {})
    selected = task_selected_at(variables, started_at)
    tasks = LocalTaskManager(db)
    selected_ancestors: set[str] = set()
    current = selected if selected in claimed else None
    while current is not None and current not in selected_ancestors:
        selected_ancestors.add(current)
        task = tasks.get_task(current)
        current = task.parent_task_id if task is not None else None
    selections: dict[str, str | None] = {}
    for checkout in LocalWorktreeManager(db).list_worktrees(project_id=project_id, limit=None):
        if checkout.status == "deleting":
            continue
        owner = checkout.task_id
        owner_task = tasks.get_task(owner) if owner is not None else None
        target = None
        if owner_task is not None and owner_task.task_type == "epic":
            if owner in selected_ancestors:
                target = selected
            elif owner in claimed:
                target = owner
        elif owner in claimed:
            target = owner
        root = os.path.realpath(checkout.worktree_path)
        selections[root] = target if root not in selections else None
    return selections


def checkout_task_at_start(
    variables: dict[str, Any], checkout_root: str | None, started_at: float | None
) -> str | None:
    """Read the frozen checkout decision, with last-selection fallback outside worktrees."""
    if started_at is not None and checkout_root is not None:
        root = os.path.realpath(checkout_root)
        calls = variables.get("task_tool_bindings", {})
        snapshots = [*calls.values(), variables.get("task_tool_turn")]
        matches = {
            call["checkout_tasks"][root]
            for call in snapshots
            if isinstance(call, dict)
            and call.get("started_at") == started_at
            and isinstance(call.get("checkout_tasks"), dict)
            and root in call["checkout_tasks"]
        }
        if matches:
            target = next(iter(matches)) if len(matches) == 1 else None
            return target if target in variables.get("claimed_tasks", {}) else None
    return (
        active_task_id_for_edit(variables)
        if started_at is None
        else task_selected_at(variables, started_at)
    )


def checkout_owned_by_other_claim(
    db: HubDatabase, variables: dict[str, Any], project_id: str, task_id: str, root: str
) -> bool:
    """Exclude another owned leaf's checkout, preserving ambiguous and foreign checks."""
    matches = [
        checkout
        for checkout in LocalWorktreeManager(db).list_worktrees(project_id=project_id, limit=None)
        if os.path.realpath(checkout.worktree_path) == os.path.realpath(root)
        and checkout.status != "deleting"
    ]
    if len(matches) != 1:
        return False
    target = matches[0].task_id
    if target is None or target == task_id or target not in variables.get("claimed_tasks", {}):
        return False
    task = LocalTaskManager(db).get_task(target)
    return task is not None and task.task_type != "epic"
