"""Repo path validation tests for task Git helper tools."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from gobby.mcp_proxy.tools import task_repo_paths
from gobby.mcp_proxy.tools.task_repo_paths import (
    CloseWorktreeRoot,
    RepoPathValidationError,
    _artifact_roots,
    resolve_close_worktree_root,
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


def _task_manager(worktree_path: str | None = None) -> SimpleNamespace:
    artifacts = SimpleNamespace(
        get_artifacts=lambda _task_id: SimpleNamespace(worktree_path=worktree_path, clone_path=None)
    )
    return SimpleNamespace(db=object(), artifacts=artifacts)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def task_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout plus a linked worktree on its own task branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "task-branch", str(worktree))
    return repo, worktree


def _close_root(*, worktree_path: str | None, commit_shas: list[str]) -> CloseWorktreeRoot:
    task = SimpleNamespace(id="task-1", project_id="project-1", parent_task_id=None)
    return resolve_close_worktree_root(
        task_manager=cast("LocalTaskManager", _task_manager(worktree_path)),
        task=cast("Task", task),
        commit_shas=commit_shas,
    )


def test_close_root_is_the_registered_worktree_holding_the_linked_commit(
    task_worktree: tuple[Path, Path],
) -> None:
    _repo, worktree = task_worktree
    branch_commit = _commit(worktree, "feature")

    root = _close_root(worktree_path=str(worktree), commit_shas=[branch_commit])

    assert root.applies
    assert root.repo_path == str(worktree)
    assert root.worktree_path == str(worktree)
    assert root.skip_reason is None


def test_close_root_skips_the_worktree_that_cannot_reach_the_linked_commit(
    task_worktree: tuple[Path, Path],
) -> None:
    repo, worktree = task_worktree
    main_only = _commit(repo, "main-only")

    root = _close_root(worktree_path=str(worktree), commit_shas=[main_only])

    assert not root.applies
    assert root.repo_path is None
    assert root.worktree_path == str(worktree)
    assert root.skip_reason == (
        f"registered worktree {worktree} was not used: linked commit {main_only} "
        "is not reachable from its HEAD"
    )


def test_close_root_names_a_deleted_registered_worktree(tmp_path: Path) -> None:
    gone = tmp_path / "gone"

    root = _close_root(worktree_path=str(gone), commit_shas=["abc123"])

    assert root.repo_path is None
    assert root.worktree_path == str(gone)
    assert root.skip_reason == f"registered worktree does not exist: {gone} (not used)"


def test_close_root_needs_a_registration_and_a_linked_commit(
    task_worktree: tuple[Path, Path],
) -> None:
    _repo, worktree = task_worktree

    unregistered = _close_root(worktree_path=None, commit_shas=["abc123"])
    uncommitted = _close_root(worktree_path=str(worktree), commit_shas=[])

    assert unregistered == CloseWorktreeRoot(
        None, None, "the task has no registered isolation worktree"
    )
    assert uncommitted.repo_path is None
    assert uncommitted.skip_reason == (
        f"registered worktree {worktree} was not used: "
        "the close names no linked commit to locate there"
    )


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
