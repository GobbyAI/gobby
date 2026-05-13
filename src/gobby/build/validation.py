"""Validation helpers for build service options and artifacts."""

from __future__ import annotations

import logging
import os

from gobby.build.options import BuildOptions
from gobby.config.build import Isolation
from gobby.storage.tasks import LocalTaskManager, Task, TaskArtifacts

logger = logging.getLogger(__name__)


def _validate_no_merge(opts: BuildOptions) -> None:
    if opts.no_merge and opts.isolation == "none":
        raise ValueError("--no-merge requires worktree or clone build workspace backend")


def _validate_clones_dir(opts: BuildOptions) -> None:
    if opts.isolation != "clone" or opts.clones_dir is None:
        return
    if not opts.clones_dir.exists() or not opts.clones_dir.is_dir():
        logger.info(
            "Rejected clone isolation because clones_dir is missing or not a directory",
            extra={"clones_dir": str(opts.clones_dir)},
        )
        raise ValueError("clones_dir must exist and be a directory for clone isolation")
    if not os.access(opts.clones_dir, os.W_OK):
        logger.info(
            "Rejected clone isolation because clones_dir is not writable",
            extra={"clones_dir": str(opts.clones_dir)},
        )
        raise ValueError("clones_dir must be writable for clone isolation")


def _validate_retry_caps(opts: BuildOptions) -> None:
    if opts.max_retries is not None and opts.max_retries < 0:
        raise ValueError("max_retries must be greater than or equal to 0")
    for override in opts.stage_caps:
        if override.max_work_attempts is not None and override.max_work_attempts < 1:
            raise ValueError(
                f"stage_caps.{override.stage_name}.max_work_attempts must be greater than or equal to 1"
            )
        if override.max_review_rounds is not None and override.max_review_rounds < 1:
            raise ValueError(
                f"stage_caps.{override.stage_name}.max_review_rounds must be greater than or equal to 1"
            )


def _validate_max_active_agents(opts: BuildOptions) -> None:
    if opts.max_active_agents is not None and opts.max_active_agents < 1:
        raise ValueError("max_active_agents must be greater than or equal to 1")


def _validate_epic_isolation_artifacts(isolation: Isolation, artifacts: TaskArtifacts) -> None:
    if isolation == "clone" and artifacts.worktree_path:
        logger.info(
            "Rejected clone isolation because a worktree artifact already exists",
            extra={"worktree_path": str(artifacts.worktree_path)},
        )
        raise ValueError(
            "task already has a worktree artifact; clear existing build artifacts before "
            "switching to clone isolation"
        )
    if isolation == "worktree" and artifacts.clone_path:
        logger.info(
            "Rejected worktree isolation because a clone artifact already exists",
            extra={"clone_path": str(artifacts.clone_path)},
        )
        raise ValueError(
            "task already has a clone artifact; clear existing build artifacts before "
            "switching to worktree isolation"
        )


def _validate_task_ref_isolation_artifacts(
    task_manager: LocalTaskManager,
    task: Task,
    isolation: Isolation,
) -> None:
    artifacts = task_manager.artifacts.get_artifacts(task.id)
    _validate_epic_isolation_artifacts(isolation, artifacts)
