"""Task isolation retargeting validation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from gobby.config.build import Isolation

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager

_ISOLATION_VALUES = ("none", "worktree", "clone")
logger = logging.getLogger(__name__)


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
        logger.info(
            "Rejected task isolation retarget due to existing worktree artifact",
            extra={
                "task_id": task_id,
                "target_isolation": normalized,
                "worktree_path": str(artifacts.worktree_path),
            },
        )
        raise ValueError(
            "task already has a worktree artifact; clear existing build artifacts before "
            "switching to clone isolation"
        )
    if normalized == "worktree" and artifacts.clone_path:
        logger.info(
            "Rejected task isolation retarget due to existing clone artifact",
            extra={
                "task_id": task_id,
                "target_isolation": normalized,
                "clone_path": str(artifacts.clone_path),
            },
        )
        raise ValueError(
            "task already has a clone artifact; clear existing build artifacts before "
            "switching to worktree isolation"
        )
    return normalized
