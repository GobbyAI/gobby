"""Validation helpers for build service options and artifacts."""

from __future__ import annotations

import os

from gobby.build.options import BuildOptions
from gobby.config.build import Isolation
from gobby.storage.tasks import LocalTaskManager, Task, TaskArtifacts


def _validate_no_merge(opts: BuildOptions) -> None:
    if opts.no_merge and opts.isolation == "none":
        raise ValueError("--no-merge requires worktree or clone build workspace backend")


def _validate_clones_dir(opts: BuildOptions) -> None:
    if opts.isolation != "clone" or opts.clones_dir is None:
        return
    if not os.access(opts.clones_dir, os.W_OK):
        raise ValueError(f"clones_dir must be writable for clone isolation: {opts.clones_dir}")


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
        raise ValueError(f"task already has worktree artifact: {artifacts.worktree_path}")
    if isolation == "worktree" and artifacts.clone_path:
        raise ValueError(f"task already has clone artifact: {artifacts.clone_path}")


def _validate_task_ref_isolation_artifacts(
    task_manager: LocalTaskManager,
    task: Task,
    isolation: Isolation,
) -> None:
    artifacts = task_manager.artifacts.get_artifacts(task.id)
    _validate_epic_isolation_artifacts(isolation, artifacts)
