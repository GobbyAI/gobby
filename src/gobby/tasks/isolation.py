"""Task isolation retargeting validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from gobby.config.build import Isolation

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager

_ISOLATION_VALUES = ("none", "worktree", "clone")


def normalize_task_isolation(value: str) -> Isolation:
    """Validate and return a task isolation value."""
    if value not in _ISOLATION_VALUES:
        raise ValueError("isolation must be one of: none, worktree, clone")
    return cast(Isolation, value)


def validate_task_isolation_artifacts(
    task_manager: LocalTaskManager,
    task_id: str,
    isolation: str,
) -> Isolation:
    """Reject retargeting to an isolation family that conflicts with current artifacts."""
    normalized = normalize_task_isolation(isolation)
    if normalized == "none":
        return normalized

    artifacts = task_manager.artifacts.get_artifacts(task_id)
    if normalized == "clone" and artifacts.worktree_path:
        raise ValueError(f"task already has worktree artifact: {artifacts.worktree_path}")
    if normalized == "worktree" and artifacts.clone_path:
        raise ValueError(f"task already has clone artifact: {artifacts.clone_path}")
    return normalized
