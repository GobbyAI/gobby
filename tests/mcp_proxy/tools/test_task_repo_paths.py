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
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import CheckoutNotFoundError, OverlayRegistrationRejectedError
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from tests.fixtures.isolated_checkout import (
    insert_isolated_machine,
    insert_overlay,
    install_isolated_checkout_project,
    patch_local_machine_id,
)

if TYPE_CHECKING:
    from gobby.storage.tasks import Task

pytestmark = pytest.mark.unit


class _ProjectManager:
    def __init__(self, repo_path: Path) -> None:
        self.project = SimpleNamespace(repo_path=str(repo_path))
        self.db = object()
        self.root_path = str(repo_path)

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


def _project_manager(repo_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ProjectManager:
    manager = _ProjectManager(repo_path)
    root = str(repo_path)
    monkeypatch.setattr(task_repo_paths, "require_root", lambda *_args, **_kwargs: root)
    monkeypatch.setattr(
        task_repo_paths,
        "require_local_machine_id",
        lambda provided, **_kwargs: provided or "machine-1",
    )

    def _resolve(
        _db: object,
        _project_id: str,
        _machine_id: str,
        overlay_path: str | None = None,
    ) -> str:
        if overlay_path in (None, root):
            return root
        raise OverlayRegistrationRejectedError(str(overlay_path))

    monkeypatch.setattr(task_repo_paths, "resolve_operation_root", _resolve)
    monkeypatch.setattr(
        task_repo_paths,
        "LocalProjectCheckoutManager",
        lambda _db: SimpleNamespace(
            list_for_machine=lambda _machine_id: [SimpleNamespace(root_path=root)]
        ),
    )
    return manager


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


def test_resolve_project_repo_path_accepts_registered_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    nested = repo_path / "nested"
    nested.mkdir(parents=True)

    result = resolve_project_repo_path(
        project_manager=_project_manager(repo_path, monkeypatch),
        project_path=str(nested),
    )

    assert result == str(nested)


def test_resolve_task_repo_path_names_project_record_for_missing_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_repo = tmp_path / "missing"
    task = SimpleNamespace(project_id="project-1", id="task-1", parent_task_id=None)

    with pytest.raises(
        RepoPathValidationError,
        match=r"project checkout root does not exist:",
    ):
        resolve_task_repo_path(
            task_manager=_task_manager(),
            project_manager=_project_manager(missing_repo, monkeypatch),
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
        project_manager=_project_manager(missing_repo, monkeypatch),
        task=task,
        project_path=str(explicit_path),
    )

    assert result == str(explicit_path)


def test_resolve_project_repo_path_rejects_symlinked_final_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    real_path = repo_path / "real"
    link_path = repo_path / "link"
    real_path.mkdir(parents=True)
    link_path.symlink_to(real_path, target_is_directory=True)

    with pytest.raises(RepoPathValidationError, match="contains symlink component"):
        resolve_project_repo_path(
            project_manager=_project_manager(repo_path, monkeypatch),
            project_path=str(link_path),
        )


def test_resolve_project_repo_path_rejects_symlinked_parent_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            project_manager=_project_manager(repo_path, monkeypatch),
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
        project_manager=_project_manager(task_repo, monkeypatch),
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
        project_manager=cast("LocalProjectManager", _project_manager(task_repo, monkeypatch)),
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
            project_manager=cast("LocalProjectManager", _project_manager(task_repo, monkeypatch)),
            task=cast("Task", task),
            project_path=str(arbitrary_repo),
        )

    assert worktree_manager.calls == [{"status": "active", "limit": 1000}]
    assert clone_manager.calls == [{"status": "active", "limit": 1000}]


def _checkout_task(
    temp_db: HubDatabase, project_id: str
) -> tuple[LocalTaskManager, LocalProjectManager, SimpleNamespace]:
    task_manager = LocalTaskManager(temp_db)
    project_manager = LocalProjectManager(temp_db)
    task = SimpleNamespace(
        id="11111111-1111-4111-8111-111111110001",
        project_id=project_id,
        parent_task_id=None,
    )
    return task_manager, project_manager, task


def test_resolve_task_repo_path_uses_machine_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    task_manager, project_manager, task = _checkout_task(temp_db, isolated.project.id)

    result = resolve_task_repo_path(
        task_manager=task_manager,
        project_manager=project_manager,
        task=cast("Task", task),
        project_path=None,
    )

    assert result == isolated.root_path


def test_resolve_task_repo_path_fails_closed_without_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="no-checkout")
    task_manager, project_manager, task = _checkout_task(temp_db, project.id)

    with pytest.raises(CheckoutNotFoundError):
        resolve_task_repo_path(
            task_manager=task_manager,
            project_manager=project_manager,
            task=cast("Task", task),
            project_path=None,
        )


def test_resolve_task_repo_path_prefers_registered_overlay(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = tmp_path / "wt"
    overlay.mkdir()
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="overlay-only")
    insert_overlay(
        temp_db,
        project_id=project.id,
        machine_id=machine_id,
        path=str(overlay),
        kind="worktree",
    )
    task_manager, project_manager, task = _checkout_task(temp_db, project.id)

    error: Exception | None = None
    result: str | None = None
    try:
        result = resolve_task_repo_path(
            task_manager=task_manager,
            project_manager=project_manager,
            task=cast("Task", task),
            project_path=str(overlay),
        )
    except RepoPathValidationError as exc:
        error = exc
    assert error is None
    assert result == str(overlay)


def test_resolve_task_repo_path_refuses_foreign_session_machine(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    foreign = insert_isolated_machine(temp_db)
    task_manager, project_manager, task = _checkout_task(temp_db, isolated.project.id)

    with pytest.raises(MachineOwnershipMismatchError) as exc_info:
        resolve_task_repo_path(
            task_manager=task_manager,
            project_manager=project_manager,
            task=cast("Task", task),
            project_path=None,
            machine_id=foreign,
        )

    assert exc_info.value.owner_machine_id == foreign
    assert exc_info.value.current_machine_id == isolated.machine_id
    assert exc_info.value.resource_kind == "project_checkout"


def test_resolve_project_repo_path_uses_machine_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )

    result = resolve_project_repo_path(
        project_manager=LocalProjectManager(temp_db),
        project_path=None,
        project_id=isolated.project.id,
    )

    assert result == isolated.root_path


def test_get_project_repo_path_uses_machine_checkout(  # tdd-red window
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    ctx = RegistryContext(task_manager=LocalTaskManager(temp_db))

    result = ctx.get_project_repo_path(isolated.project.id)

    assert result == isolated.root_path


def test_get_project_repo_path_fails_closed_without_checkout(  # tdd-red window
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create(name="ctx-no-checkout")
    ctx = RegistryContext(task_manager=LocalTaskManager(temp_db))

    with pytest.raises(CheckoutNotFoundError):
        ctx.get_project_repo_path(project.id)
