"""Repo path validation tests for task Git helper tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.task_repo_paths import (
    RepoPathValidationError,
    _artifact_roots,
    resolve_project_repo_path,
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
