"""Repo path validation tests for task Git helper tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from gobby.mcp_proxy.tools import task_repo_paths
from gobby.mcp_proxy.tools.task_repo_paths import (
    RepoPathValidationError,
    _artifact_roots,
    resolve_project_repo_path,
    resolve_task_repo_path,
)

if TYPE_CHECKING:
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit


class _ProjectManager:
    def __init__(self, repo_path: Path) -> None:
        self.project = SimpleNamespace(repo_path=str(repo_path))

    def get(self, _project_id: str | None) -> SimpleNamespace:
        return self.project

    def list(self) -> list[SimpleNamespace]:
        return [self.project]


class _IsolationManager:
    def __init__(self, records: list[SimpleNamespace]) -> None:
        self.records = records
        self.calls: list[dict[str, Any]] = []

    def list_worktrees(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.append(kwargs)
        return self.records

    def list_clones(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.append(kwargs)
        return self.records


def _project_manager(repo_path: Path) -> _ProjectManager:
    return _ProjectManager(repo_path)


def _task_manager() -> SimpleNamespace:
    artifacts = SimpleNamespace(
        get_artifacts=lambda _task_id: SimpleNamespace(worktree_path=None, clone_path=None)
    )
    return SimpleNamespace(db=object(), artifacts=artifacts)


def test_resolve_project_repo_path_accepts_registered_descendant(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    nested = repo_path / "nested"
    nested.mkdir(parents=True)

    result = resolve_project_repo_path(
        project_manager=_project_manager(repo_path),
        project_path=str(nested),
    )

    assert result == str(nested)


def test_resolve_task_repo_path_names_project_record_for_missing_repo(tmp_path: Path) -> None:
    missing_repo = tmp_path / "missing"
    task = SimpleNamespace(project_id="project-1", id="task-1", parent_task_id=None)

    with pytest.raises(
        RepoPathValidationError,
        match=r"project record repo_path does not exist:",
    ):
        resolve_task_repo_path(
            task_manager=_task_manager(),
            project_manager=_project_manager(missing_repo),
            task=task,
            project_path=None,
        )


def test_resolve_task_repo_path_honors_explicit_path_when_project_record_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_path = tmp_path / "registered-worktree"
    explicit_path.mkdir()
    missing_repo = tmp_path / "missing-canonical"
    task = SimpleNamespace(project_id="project-1", id="task-1", parent_task_id=None)
    worktree_manager = _IsolationManager([SimpleNamespace(worktree_path=str(explicit_path))])
    monkeypatch.setattr(task_repo_paths, "LocalWorktreeManager", lambda _db: worktree_manager)
    monkeypatch.setattr(
        task_repo_paths,
        "LocalCloneManager",
        lambda _db: _IsolationManager([]),
    )

    result = resolve_task_repo_path(
        task_manager=_task_manager(),
        project_manager=_project_manager(missing_repo),
        task=task,
        project_path=str(explicit_path),
    )

    assert result == str(explicit_path)


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
    def fail(_task_id: str) -> None:
        raise ValueError("artifact storage failed")

    task_manager = SimpleNamespace(artifacts=SimpleNamespace(get_artifacts=fail))

    with pytest.raises(ValueError, match="artifact storage failed"):
        list(_artifact_roots(task_manager, "task-1"))


def test_resolve_task_repo_path_accepts_active_external_project_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_repo = tmp_path / "task-repo"
    external_worktree = tmp_path / "external-project" / "worktree"
    task_repo.mkdir()
    external_worktree.mkdir(parents=True)
    task = SimpleNamespace(id="task-1", project_id="task-project", parent_task_id=None)
    worktree = SimpleNamespace(task_id="sibling-task", worktree_path=str(external_worktree))
    worktree_manager = _IsolationManager([worktree])
    clone_manager = _IsolationManager([])
    monkeypatch.setattr(task_repo_paths, "LocalWorktreeManager", lambda _db: worktree_manager)
    monkeypatch.setattr(task_repo_paths, "LocalCloneManager", lambda _db: clone_manager)

    result = resolve_task_repo_path(
        task_manager=_task_manager(),
        project_manager=_project_manager(task_repo),
        task=task,
        project_path=str(external_worktree),
    )

    assert result == str(external_worktree)
    assert worktree_manager.calls == [{"status": "active", "limit": 1000}]
    assert clone_manager.calls == [{"status": "active", "limit": 1000}]


def test_resolve_task_repo_path_accepts_active_external_project_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_repo = tmp_path / "task-repo"
    external_clone = tmp_path / "external-project" / "clone"
    task_repo.mkdir()
    external_clone.mkdir(parents=True)
    task = SimpleNamespace(id="task-1", project_id="task-project", parent_task_id=None)
    clone = SimpleNamespace(task_id="sibling-task", clone_path=str(external_clone))
    worktree_manager = _IsolationManager([])
    clone_manager = _IsolationManager([clone])
    monkeypatch.setattr(task_repo_paths, "LocalWorktreeManager", lambda _db: worktree_manager)
    monkeypatch.setattr(task_repo_paths, "LocalCloneManager", lambda _db: clone_manager)

    result = resolve_task_repo_path(
        task_manager=cast("LocalTaskManager", _task_manager()),
        project_manager=cast("LocalProjectManager", _project_manager(task_repo)),
        task=cast("Task", task),
        project_path=str(external_clone),
    )

    assert result == str(external_clone)
    assert worktree_manager.calls == [{"status": "active", "limit": 1000}]
    assert clone_manager.calls == [{"status": "active", "limit": 1000}]


def test_resolve_task_repo_path_rejects_unregistered_external_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_repo = tmp_path / "task-repo"
    registered_worktree = tmp_path / "registered-project" / "worktree"
    arbitrary_repo = tmp_path / "arbitrary-repo"
    task_repo.mkdir()
    registered_worktree.mkdir(parents=True)
    arbitrary_repo.mkdir()
    task = SimpleNamespace(id="task-1", project_id="task-project", parent_task_id=None)
    worktree = SimpleNamespace(task_id="sibling-task", worktree_path=str(registered_worktree))
    worktree_manager = _IsolationManager([worktree])
    clone_manager = _IsolationManager([])
    monkeypatch.setattr(task_repo_paths, "LocalWorktreeManager", lambda _db: worktree_manager)
    monkeypatch.setattr(task_repo_paths, "LocalCloneManager", lambda _db: clone_manager)

    with pytest.raises(RepoPathValidationError, match="outside the task project repo"):
        resolve_task_repo_path(
            task_manager=cast("LocalTaskManager", _task_manager()),
            project_manager=cast("LocalProjectManager", _project_manager(task_repo)),
            task=cast("Task", task),
            project_path=str(arbitrary_repo),
        )

    assert worktree_manager.calls == [{"status": "active", "limit": 1000}]
    assert clone_manager.calls == [{"status": "active", "limit": 1000}]
