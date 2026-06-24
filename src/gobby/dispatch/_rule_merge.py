"""Workspace merge decisions for dispatch rules."""

from __future__ import annotations

from gobby.dispatch._rule_state import (
    _artifacts,
    _field,
    _matching_current_stage,
    _task_has_label,
    _task_id,
    _task_ref,
)
from gobby.dispatch.actions import MergeWorkspaceAction
from gobby.dispatch.merge_recovery import WORKSPACE_MERGE_CONFLICT_LABEL


def _workspace_merge_action(task: object, context: object) -> MergeWorkspaceAction | None:
    stage = _matching_current_stage(task, context, "merge", "in_progress")
    if stage is None or not _has_workspace_merge_source(task, context):
        return None
    if _task_has_label(task, WORKSPACE_MERGE_CONFLICT_LABEL):
        return None
    artifacts = _artifacts(task, context)
    target_branch = _field(artifacts, "target_branch")
    if not isinstance(target_branch, str):
        raise TypeError("workspace merge action requires string target_branch")

    integration_branch = _field(artifacts, "integration_branch")
    integration_workspace_id = _field(artifacts, "integration_workspace_id")
    integration_clone_id = _field(artifacts, "integration_clone_id")
    worktree_id = _field(artifacts, "worktree_id")
    clone_id = _field(artifacts, "clone_id")

    if isinstance(integration_branch, str) and isinstance(integration_clone_id, str):
        return MergeWorkspaceAction(
            task_id=_task_id(task),
            task_ref=_task_ref(task),
            backend="clone",
            target_branch=target_branch,
            source_branch=integration_branch,
            source_clone_id=integration_clone_id,
        )
    if isinstance(integration_branch, str) and isinstance(integration_workspace_id, str):
        return MergeWorkspaceAction(
            task_id=_task_id(task),
            task_ref=_task_ref(task),
            backend="worktree",
            target_branch=target_branch,
            source_branch=integration_branch,
            source_workspace_id=integration_workspace_id,
        )
    if isinstance(clone_id, str):
        return MergeWorkspaceAction(
            task_id=_task_id(task),
            task_ref=_task_ref(task),
            backend="clone",
            target_branch=target_branch,
            source_clone_id=clone_id,
        )
    if isinstance(worktree_id, str):
        return MergeWorkspaceAction(
            task_id=_task_id(task),
            task_ref=_task_ref(task),
            backend="worktree",
            target_branch=target_branch,
            source_workspace_id=worktree_id,
        )
    return None


def _has_workspace_merge_source(task: object, context: object) -> bool:
    artifacts = _artifacts(task, context)
    target_branch = _field(artifacts, "target_branch")
    if not isinstance(target_branch, str) or not target_branch:
        return False
    has_parent = bool(_field(task, "parent_task_id"))
    source_fields = (
        (
            "integration_workspace_id",
            "integration_clone_id",
            "worktree_id",
            "clone_id",
        )
        if has_parent
        else ("integration_workspace_id", "integration_clone_id")
    )
    return any(isinstance(_field(artifacts, field_name), str) for field_name in source_fields)


__all__ = ["_has_workspace_merge_source", "_workspace_merge_action"]
