"""Task isolation retargeting validation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from gobby.config.build import CheckoutMode

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager

_CHECKOUT_MODE_VALUES = ("none", "worktree", "clone")
logger = logging.getLogger(__name__)


def normalize_task_checkout_mode(value: str) -> CheckoutMode:
    """Validate and return a task checkout mode."""
    if value not in _CHECKOUT_MODE_VALUES:
        raise ValueError("checkout_mode must be one of: none, worktree, clone")
    return cast(CheckoutMode, value)


def validate_task_isolation_artifacts(
    task_manager: LocalTaskManager,
    task_id: str,
    checkout_mode: str,
) -> CheckoutMode:
    """Reject retargeting to an isolation family that conflicts with current artifacts."""
    normalized = normalize_task_checkout_mode(checkout_mode)
    if normalized == "none":
        return normalized

    artifacts = task_manager.artifacts.get_artifacts(task_id)
    if normalized == "clone" and artifacts.worktree_path:
        logger.info(
            "Rejected task isolation retarget due to existing worktree artifact",
            extra={
                "task_id": task_id,
                "target_checkout_mode": normalized,
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
                "target_checkout_mode": normalized,
                "clone_path": str(artifacts.clone_path),
            },
        )
        raise ValueError(
            "task already has a clone artifact; clear existing build artifacts before "
            "switching to worktree isolation"
        )
    return normalized
