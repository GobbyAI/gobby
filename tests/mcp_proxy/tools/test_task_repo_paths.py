"""Repo path validation tests for task Git helper tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.task_repo_paths import (
    RepoPathValidationError,
    _artifact_roots,
    resolve_project_repo_path,
    resolve_task_repo_path,
)

pytestmark = pytest.mark.unit


def _project_manager(repo_path: Path) -> MagicMock:
    manager = MagicMock()
    project = MagicMock(repo_path=str(repo_path))
    manager.get.return_value = project
    manager.list.return_value = [project]
    return manager


def test_resolve_project_repo_path_accepts_registered_descendant(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    nested = repo_path / "nested"
    nested.mkdir(parents=True)

    result = resolve_project_repo_path(
        project_manager=_project_manager(repo_path),
        project_path=str(nested),
    )

    assert result == str(nested)


def test_resolve_project_repo_path_rejects_symlinked_final_component(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    real_path = repo_path / "real"
    link_path = repo_path / "link"
    real_path.mkdir(parents=True)
    link_path.symlink_to(real_path, target_is_directory=True)

    with pytest.raises(RepoPathValidationError, match="contains symlink component"):
        resolve_project_repo_path(
            project_manager=_project_manager(repo_path),
            project_path=str(link_path),
        )


def test_resolve_project_repo_path_rejects_symlinked_parent_component(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    outside_parent = tmp_path / "outside"
    outside_child = outside_parent / "child"
    outside_child.mkdir(parents=True)
    linked_parent = repo_path / "linked-parent"
    linked_parent.symlink_to(outside_parent, target_is_directory=True)

    with pytest.raises(RepoPathValidationError, match="contains symlink component"):
        resolve_project_repo_path(
            project_manager=_project_manager(repo_path),
            project_path=str(linked_parent / "child"),
        )


def test_artifact_roots_propagates_get_artifacts_errors() -> None:
    task_manager = MagicMock()
    task_manager.artifacts.get_artifacts.side_effect = ValueError("artifact storage failed")

    with pytest.raises(ValueError, match="artifact storage failed"):
        list(_artifact_roots(task_manager, "task-1"))


def _task_path_manager(*tasks: MagicMock) -> MagicMock:
    task_manager = MagicMock()
    tasks_by_id = {task.id: task for task in tasks}
    task_manager.get_task.side_effect = tasks_by_id.__getitem__
    task_manager.artifacts.get_artifacts.return_value = SimpleNamespace(
        worktree_path=None,
        clone_path=None,
    )
    return task_manager


def test_resolve_task_repo_path_accepts_registered_shared_epic_worktree(
    tmp_path: Path,
) -> None:
    project_repo = tmp_path / "project"
    project_repo.mkdir()
    sibling_repo = tmp_path / "sibling-worktree"
    sibling_repo.mkdir()

    root = MagicMock(id="epic-root", parent_task_id=None, task_type="epic")
    target = MagicMock(
        id="coordination-fix",
        parent_task_id=root.id,
        project_id="project-1",
        task_type="bug",
    )
    sibling = MagicMock(
        id="sibling-leaf",
        parent_task_id=root.id,
        project_id="project-1",
        task_type="bug",
    )
    task_manager = _task_path_manager(root, target, sibling)
    worktree = MagicMock(task_id=sibling.id, worktree_path=str(sibling_repo))

    with (
        patch("gobby.mcp_proxy.tools.task_repo_paths.LocalWorktreeManager") as worktrees,
        patch("gobby.mcp_proxy.tools.task_repo_paths.LocalCloneManager") as clones,
    ):
        worktrees.return_value.list_worktrees.return_value = [worktree]
        clones.return_value.list_clones.return_value = []

        result = resolve_task_repo_path(
            task_manager=task_manager,
            project_manager=_project_manager(project_repo),
            task=target,
            project_path=str(sibling_repo),
        )

    assert result == str(sibling_repo)
    assert worktrees.call_count == 1
    assert worktrees.return_value.list_worktrees.call_count == 1
    assert clones.call_count == 1
    assert task_manager.get_task.call_count >= 3
    worktrees.assert_called_once_with(task_manager.db)
    worktrees.return_value.list_worktrees.assert_called_once_with(
        project_id="project-1",
        limit=1000,
    )
    task_manager.get_task.assert_any_call(sibling.id)
    task_manager.get_task.assert_any_call(root.id)


def test_resolve_task_repo_path_rejects_registered_unrelated_epic_worktree(
    tmp_path: Path,
) -> None:
    project_repo = tmp_path / "project"
    project_repo.mkdir()
    unrelated_repo = tmp_path / "unrelated-worktree"
    unrelated_repo.mkdir()

    root = MagicMock(id="epic-root", parent_task_id=None, task_type="epic")
    unrelated_root = MagicMock(id="other-epic", parent_task_id=None, task_type="epic")
    target = MagicMock(
        id="coordination-fix",
        parent_task_id=root.id,
        project_id="project-1",
        task_type="bug",
    )
    unrelated = MagicMock(
        id="unrelated-leaf",
        parent_task_id=unrelated_root.id,
        project_id="project-1",
        task_type="bug",
    )
    task_manager = _task_path_manager(root, unrelated_root, target, unrelated)
    worktree = MagicMock(task_id=unrelated.id, worktree_path=str(unrelated_repo))

    with (
        patch("gobby.mcp_proxy.tools.task_repo_paths.LocalWorktreeManager") as worktrees,
        patch("gobby.mcp_proxy.tools.task_repo_paths.LocalCloneManager") as clones,
    ):
        worktrees.return_value.list_worktrees.return_value = [worktree]
        clones.return_value.list_clones.return_value = []

        with pytest.raises(RepoPathValidationError, match="outside the task project repo"):
            resolve_task_repo_path(
                task_manager=task_manager,
                project_manager=_project_manager(project_repo),
                task=target,
                project_path=str(unrelated_repo),
            )

    worktrees.assert_called_once_with(task_manager.db)
    assert worktrees.call_count == 1
    assert worktrees.return_value.list_worktrees.call_count == 1
    assert clones.call_count == 1
    assert task_manager.get_task.call_count >= 3
    worktrees.return_value.list_worktrees.assert_called_once_with(
        project_id="project-1",
        limit=1000,
    )
    task_manager.get_task.assert_any_call(unrelated.id)
    task_manager.get_task.assert_any_call(unrelated_root.id)
