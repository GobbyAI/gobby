from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from gobby.build.workspaces import BuildWorkspaceError, _integration_branch
from gobby.dispatch.actions import MergeWorkspaceAction
from gobby.dispatch.merge_recovery import WORKSPACE_MERGE_CONFLICT_LABEL
from gobby.dispatch.workspace_merge import (
    _acquire_integration_mutex,
    _non_gobby_status_lines,
    _sync_source_repo_branch,
    execute_merge_workspace,
)
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase, IntegrationWorkspaceMutex
from gobby.storage.projects import LocalProjectManager, Project
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("initial\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _merge_checkout(
    temp_db: HubDatabase,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "merge-project",
) -> Project:
    from tests.fixtures.isolated_checkout import (
        IsolatedCheckoutProject,
        install_isolated_checkout_project,
    )

    isolated: IsolatedCheckoutProject = install_isolated_checkout_project(
        temp_db, repo, name=name, monkeypatch=monkeypatch
    )
    monkeypatch.setattr(
        "gobby.storage.worktrees.require_machine_id",
        lambda: isolated.machine_id,
    )
    monkeypatch.setattr(
        "gobby.storage.clones.require_machine_id",
        lambda: isolated.machine_id,
    )
    return isolated.project


def _assert_worktree_removed(
    worktrees: LocalWorktreeManager,
    worktree_id: str,
    worktree_path: Path,
) -> None:
    assert worktrees.get(worktree_id) is None
    assert not worktree_path.exists()


class _LeaseTransaction:
    def __init__(self, db: _ConcurrentLeaseDB, *, serialized: bool) -> None:
        self._db = db
        self._serialized = serialized
        self._row: dict[str, str] | None = None

    def execute(
        self,
        sql: str,
        params: tuple[object, ...] = (),
    ) -> _LeaseTransaction:
        key = str(params[0])
        if "SELECT lease_until" in sql:
            with self._db.state_guard:
                row = self._db.rows.get(key)
                self._row = dict(row) if row is not None else None
            if not self._serialized:
                self._db.concurrent_reads.wait(timeout=1)
            return self
        if "INSERT INTO integration_workspace_mutex" in sql:
            with self._db.state_guard:
                self._db.rows[key] = {
                    "lease_until": str(params[1]),
                    "lease_holder": str(params[2]),
                }
            return self
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> dict[str, str] | None:
        return self._row


class _ConcurrentLeaseDB:
    def __init__(self) -> None:
        self.concurrent_reads = threading.Barrier(2)
        self.state_guard = threading.Lock()
        self.transaction_guard = threading.Lock()
        self.rows: dict[str, dict[str, str]] = {}
        self.seen_locks: list[object | None] = []

    @contextmanager
    def transaction_immediate(
        self,
        lock: object | None = None,
    ) -> Iterator[_LeaseTransaction]:
        self.seen_locks.append(lock)
        if lock is None:
            yield _LeaseTransaction(self, serialized=False)
            return
        with self.transaction_guard:
            yield _LeaseTransaction(self, serialized=True)


async def test_integration_mutex_allows_exactly_one_concurrent_lease_holder() -> None:
    db = _ConcurrentLeaseDB()
    callers_ready = threading.Barrier(2)

    def acquire() -> bool:
        callers_ready.wait(timeout=1)
        return _acquire_integration_mutex(db, "epic:123")  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: acquire(), range(2)))

    assert sorted(results) == [False, True]
    assert db.seen_locks == [
        IntegrationWorkspaceMutex(integration_key="epic:123"),
        IntegrationWorkspaceMutex(integration_key="epic:123"),
    ]


async def test_non_gobby_status_lines_ignores_gobby_paths_with_full_or_stripped_prefix() -> None:
    assert _non_gobby_status_lines(" M .gobby/tasks.jsonl\n") == []
    assert _non_gobby_status_lines("M .gobby/tasks.jsonl") == []
    assert _non_gobby_status_lines("R  .gobby/old.json -> .gobby/new.json") == []
    assert _non_gobby_status_lines("M src/gobby/app.py\n M .gobby/tasks.jsonl") == [
        "M src/gobby/app.py"
    ]


async def test_execute_merge_workspace_merges_worktree_and_completes_stage(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert (integration_path / "feature.txt").read_text() == "feature\n"
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None


async def test_execute_merge_workspace_recovers_interrupted_target_merge(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    (repo / "conflict.txt").write_text("base\n")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "base conflict file")
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    for path in (repo, integration_path, task_path):
        _git(path, "config", "user.email", "test@example.com")
        _git(path, "config", "user.name", "Test User")

    (repo / "conflict.txt").write_text("stale source\n")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "stale source change")
    (integration_path / "conflict.txt").write_text("integration\n")
    _git(integration_path, "add", "conflict.txt")
    _git(integration_path, "commit", "-m", "integration change")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

    interrupted = subprocess.run(
        ["git", "merge", "main"],
        cwd=integration_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert interrupted.returncode != 0
    assert _git(integration_path, "rev-parse", "--verify", "MERGE_HEAD")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert (integration_path / "conflict.txt").read_text() == "integration\n"
    assert (integration_path / "feature.txt").read_text() == "feature\n"
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "MERGE_HEAD"],
            cwd=integration_path,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


async def test_execute_merge_workspace_completes_already_merged_worktree(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")
    source_commit = _git(task_path, "rev-parse", "HEAD")
    _git(integration_path, "merge", "--no-ff", "--no-edit", source_commit)

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert merge_sha != source_commit
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None


async def test_execute_merge_workspace_retries_clone_sync_before_completing_stage(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration-clone"
    task_path = tmp_path / "task-clone"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "branch", "integration/root", "main")
    _git(tmp_path, "clone", "--branch", "integration/root", str(repo), str(integration_path))
    _git(tmp_path, "clone", "--branch", "integration/root", str(repo), str(task_path))
    _git(task_path, "checkout", "-b", "task/leaf")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    clones = LocalCloneManager(temp_db)
    clones.create(
        project_id=project.id,
        branch_name="integration/root",
        clone_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = clones.create(
        project_id=project.id,
        branch_name="task/leaf",
        clone_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        clone_path=str(task_path),
        clone_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )
    action = MergeWorkspaceAction(
        task_id=leaf.id,
        task_ref=f"#{leaf.seq_num}",
        backend="clone",
        target_branch="integration/root",
        source_clone_id=source.id,
    )

    _git(repo, "checkout", "integration/root")
    first_result = await execute_merge_workspace(action, db=temp_db)

    integration_sha = _git(integration_path, "rev-parse", "HEAD")
    failed_task = task_manager.get_task(leaf.id)
    assert first_result is None
    assert failed_task is not None
    assert failed_task.is_escalated is True
    assert _git(repo, "rev-parse", "integration/root") != integration_sha

    _git(repo, "checkout", "main")
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")
    retry_result = await execute_merge_workspace(action, db=temp_db)

    stage = task_manager.stage_states.get(leaf.id, "merge")
    assert retry_result == integration_sha
    assert stage is not None
    assert stage.state == "done"
    assert stage.completed_commit_sha == integration_sha
    assert _git(repo, "merge-base", "--is-ancestor", integration_sha, "integration/root") == ""
    stored_source = clones.get(source.id)
    assert stored_source is not None
    assert stored_source.status == "merged"

    _git(repo, "checkout", "integration/root")
    _sync_source_repo_branch(
        temp_db,
        leaf.id,
        str(integration_path),
        "integration/root",
    )


async def test_execute_merge_workspace_escalates_non_ff_clone_sync(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration-clone"
    task_path = tmp_path / "task-clone"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "branch", "integration/root", "main")
    _git(tmp_path, "clone", "--branch", "integration/root", str(repo), str(integration_path))
    _git(tmp_path, "clone", "--branch", "integration/root", str(repo), str(task_path))
    _git(task_path, "checkout", "-b", "task/leaf")
    for clone_path in (integration_path, task_path):
        _git(clone_path, "config", "user.email", "test@example.com")
        _git(clone_path, "config", "user.name", "Test User")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")
    _git(repo, "checkout", "integration/root")
    (repo / "user-change.txt").write_text("user change\n")
    _git(repo, "add", "user-change.txt")
    _git(repo, "commit", "-m", "user change")
    user_branch_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    clones = LocalCloneManager(temp_db)
    clones.create(
        project_id=project.id,
        branch_name="integration/root",
        clone_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = clones.create(
        project_id=project.id,
        branch_name="task/leaf",
        clone_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        clone_path=str(task_path),
        clone_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    result = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="clone",
            target_branch="integration/root",
            source_clone_id=source.id,
        ),
        db=temp_db,
    )

    integration_sha = _git(integration_path, "rev-parse", "HEAD")
    failed_task = task_manager.get_task(leaf.id)
    stage = task_manager.stage_states.get(leaf.id, "merge")
    assert result is None
    assert failed_task is not None
    assert failed_task.is_escalated is True
    assert stage is not None
    assert stage.state == "ready"
    assert _git(repo, "rev-parse", "integration/root") == user_branch_sha
    merge_base = _git(repo, "merge-base", integration_sha, "integration/root")
    assert merge_base != integration_sha


async def test_execute_merge_workspace_lands_root_integration_worktree_on_local_branch(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    dirty_task_path = tmp_path / "dirty-task"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "worktree", "add", "-b", "gobby/integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/stale-leaf", str(task_path), "main")
    _git(repo, "worktree", "add", "-b", "task/dirty-leaf", str(dirty_task_path), "main")
    (dirty_task_path / "dirty.txt").write_text("dirty\n")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    (integration_path / "feature.txt").write_text("feature\n")
    _git(integration_path, "add", "feature.txt")
    _git(integration_path, "commit", "-m", "feature")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project_id=project.id,
        title="Root",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Stale leaf",
        parent_task_id=root.id,
        category="docs",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    dirty_leaf = task_manager.create_task(
        project_id=project.id,
        title="Dirty leaf",
        parent_task_id=root.id,
        category="docs",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(root.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(root.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    integration = worktrees.create(
        project_id=project.id,
        branch_name="gobby/integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=root.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/stale-leaf",
        worktree_path=str(task_path),
        base_branch="main",
        task_id=leaf.id,
    )
    dirty_source = worktrees.create(
        project_id=project.id,
        branch_name="task/dirty-leaf",
        worktree_path=str(dirty_task_path),
        base_branch="main",
        task_id=dirty_leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        root.id,
        target_branch="main",
        integration_branch="gobby/integration/root",
        integration_workspace_id=integration.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="gobby/integration/root",
    )
    task_manager.artifacts.set_artifacts_atomic(
        dirty_leaf.id,
        worktree_path=str(dirty_task_path),
        worktree_id=dirty_source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="gobby/integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=root.id,
            task_ref=f"#{root.seq_num}",
            backend="worktree",
            target_branch="main",
            source_branch="gobby/integration/root",
            source_workspace_id=integration.id,
        ),
        db=temp_db,
    )

    stage = task_manager.stage_states.get(root.id, "merge")

    assert merge_sha == _git(repo, "rev-parse", "HEAD")
    assert merge_sha == _git(repo, "rev-parse", "main")
    assert (repo / "feature.txt").read_text() == "feature\n"
    assert stage is not None
    assert stage.state == "done"
    assert stage.completed_commit_sha == merge_sha
    assert stage.artifact_refs == {"integration_merge_sha": merge_sha}
    _assert_worktree_removed(worktrees, integration.id, integration_path)
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(root.id).integration_workspace_id is None
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None
    assert worktrees.get(dirty_source.id) is not None
    assert dirty_task_path.exists()
    assert task_manager.artifacts.get_artifacts(dirty_leaf.id).worktree_id == dirty_source.id


async def test_execute_merge_workspace_lands_child_epic_integration_on_local_branch(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "phase-integration"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "checkout", "-b", "gobby/integration/root")
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        "gobby/integration/phase",
        str(integration_path),
        "gobby/integration/root",
    )
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    (integration_path / "phase.txt").write_text("phase\n")
    _git(integration_path, "add", "phase.txt")
    _git(integration_path, "commit", "-m", "phase work")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    root = task_manager.create_task(
        project_id=project.id,
        title="Root",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    phase = task_manager.create_task(
        project_id=project.id,
        title="Phase",
        parent_task_id=root.id,
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(phase.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(phase.id, "merge", by_session_id="test")
    task_manager.artifacts.set_artifacts_atomic(
        root.id,
        target_branch="main",
        integration_branch="gobby/integration/root",
    )

    worktrees = LocalWorktreeManager(temp_db)
    root_integration = worktrees.create(
        project_id=project.id,
        branch_name="gobby/integration/root",
        worktree_path=str(repo),
        base_branch="main",
        task_id=root.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifact(
        root.id,
        "integration_workspace_id",
        root_integration.id,
    )
    integration = worktrees.create(
        project_id=project.id,
        branch_name="gobby/integration/phase",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=phase.id,
        workspace_role="integration",
    )
    task_manager.artifacts.set_artifacts_atomic(
        phase.id,
        target_branch="gobby/integration/root",
        integration_branch="gobby/integration/phase",
        integration_workspace_id=integration.id,
    )
    (repo / ".gobby").mkdir(exist_ok=True)
    (repo / ".gobby" / "tasks.jsonl").write_text("sync artifact\n")

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=phase.id,
            task_ref=f"#{phase.seq_num}",
            backend="worktree",
            target_branch="gobby/integration/root",
            source_branch="gobby/integration/phase",
            source_workspace_id=integration.id,
        ),
        db=temp_db,
    )

    stage = task_manager.stage_states.get(phase.id, "merge")

    assert merge_sha == _git(repo, "rev-parse", "HEAD")
    assert (repo / "phase.txt").read_text() == "phase\n"
    assert stage is not None
    assert stage.state == "done"
    assert stage.completed_commit_sha == merge_sha
    _assert_worktree_removed(worktrees, integration.id, integration_path)
    assert task_manager.artifacts.get_artifacts(phase.id).integration_workspace_id is None


async def test_execute_merge_workspace_adopts_missing_integration_worktree_metadata(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generated integration worktree metadata is adopted when the DB row is missing."""
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    integration_branch = _integration_branch(parent)

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), integration_branch)
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")
    task_manager.artifacts.set_artifacts_atomic(
        parent.id,
        target_branch="main",
    )
    worktrees = LocalWorktreeManager(temp_db)
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch=integration_branch,
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch=integration_branch,
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch=integration_branch,
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    parent_artifacts = task_manager.artifacts.get_artifacts(parent.id)
    adopted = worktrees.get_by_branch(project.id, integration_branch)

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert adopted is not None
    assert adopted.workspace_role == "integration"
    assert adopted.task_id == parent.id
    assert adopted.worktree_path == str(integration_path)
    assert parent_artifacts.integration_workspace_id == adopted.id
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None


async def test_execute_merge_workspace_rejects_dirty_unmanaged_integration_worktree(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    integration_branch = _integration_branch(parent)

    _git(repo, "worktree", "add", "-b", integration_branch, str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), integration_branch)
    (integration_path / "dirty.txt").write_text("dirty\n")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")
    task_manager.artifacts.set_artifacts_atomic(parent.id, target_branch="main")
    worktrees = LocalWorktreeManager(temp_db)
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch=integration_branch,
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch=integration_branch,
    )

    with pytest.raises(BuildWorkspaceError, match="integration workspace is dirty"):
        await execute_merge_workspace(
            MergeWorkspaceAction(
                task_id=leaf.id,
                task_ref=f"#{leaf.seq_num}",
                backend="worktree",
                target_branch=integration_branch,
                source_workspace_id=source.id,
            ),
            db=temp_db,
        )

    assert worktrees.get(source.id) is not None
    assert task_path.exists()


async def test_execute_merge_workspace_allows_disjoint_registered_target_dirt(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)

    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    (integration_path / "dirty.txt").write_text("dirty\n")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    stage = task_manager.stage_states.get(leaf.id, "merge")
    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert stage is not None
    assert stage.state == "done"
    assert (integration_path / "dirty.txt").read_text() == "dirty\n"
    _assert_worktree_removed(worktrees, source.id, task_path)


async def test_execute_merge_workspace_fails_stage_when_target_dirt_overlaps_merge(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)

    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    (integration_path / "feature.txt").write_text("dirty local feature\n")
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", "feature.txt")
    _git(task_path, "commit", "-m", "feature")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    stage = task_manager.stage_states.get(leaf.id, "merge")
    updated = task_manager.get_task(leaf.id)
    assert merge_sha is None
    assert stage is not None
    assert stage.state == "ready"
    assert (
        "### Workspace merge failed\n\n"
        "workspace_merge_failed:target integration workspace dirty paths overlap merge: feature.txt"
    ) in (updated.description or "")
    assert worktrees.get(source.id) is not None
    assert task_path.exists()


async def test_execute_merge_workspace_preserves_worktree_after_merge_conflict(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    (repo / "conflict.txt").write_text("base\n")
    _git(repo, "add", "conflict.txt")
    _git(repo, "commit", "-m", "base conflict file")
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")
    (integration_path / "conflict.txt").write_text("integration\n")
    _git(integration_path, "add", "conflict.txt")
    _git(integration_path, "commit", "-m", "integration change")
    (task_path / "conflict.txt").write_text("task\n")
    _git(task_path, "add", "conflict.txt")
    _git(task_path, "commit", "-m", "task change")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    stage = task_manager.stage_states.get(leaf.id, "merge")
    updated = task_manager.get_task(leaf.id)
    assert merge_sha is None
    assert stage is not None
    assert stage.state == "ready"
    assert not updated.is_escalated
    assert WORKSPACE_MERGE_CONFLICT_LABEL in (updated.labels or [])
    assert worktrees.get(source.id) is not None
    assert task_path.exists()
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id == source.id


async def test_execute_merge_workspace_resolves_worktree_local_project_metadata(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-generated worktree-local project metadata is preserved during merge."""
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    (repo / ".gobby").mkdir(exist_ok=True)
    project_json = (
        '{\n  "id": "aa81136a-134a-5bf3-bcd4-adac1fe28e9b",\n  "name": "merge-project"\n}\n'
    )
    (repo / ".gobby" / "project.json").write_text(project_json)
    _git(repo, "add", ".gobby/project.json")
    _git(repo, "commit", "-m", "add project metadata")
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")

    integration_project_json = '{\n  "id": "aa81136a-134a-5bf3-bcd4-adac1fe28e9b",\n  "name": "merge-project",\n  "parent_project_path": "/repo"\n}\n'
    (integration_path / ".gobby" / "project.json").write_text(integration_project_json)
    _git(integration_path, "add", ".gobby/project.json")
    _git(integration_path, "commit", "-m", "record integration project path")

    task_project_json = '{\n  "id": "aa81136a-134a-5bf3-bcd4-adac1fe28e9b",\n  "name": "merge-project",\n  "parent_project_id": "aa81136a-134a-5bf3-bcd4-adac1fe28e9b"\n}\n'
    (task_path / ".gobby" / "project.json").write_text(task_project_json)
    (task_path / "feature.txt").write_text("feature\n")
    _git(task_path, "add", ".gobby/project.json", "feature.txt")
    _git(task_path, "commit", "-m", "feature with local project metadata")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="code",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert (integration_path / "feature.txt").read_text() == "feature\n"
    assert (integration_path / ".gobby" / "project.json").read_text() == integration_project_json
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None


async def test_execute_merge_workspace_resolves_docs_guides_readme_row_conflict(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    readme = repo / "docs" / "guides" / "README.md"
    readme.parent.mkdir(parents=True)
    readme.write_text(
        "# Gobby Guides\n\n"
        "Documentation guides for using Gobby's features.\n\n"
        "## Core Features\n\n"
        "| Guide | Description |\n"
        "|-------|-------------|\n"
        "| [search.md](search.md) | Unified search with TF-IDF, embeddings, and hybrid modes |\n"
        "| [tasks.md](tasks.md) | Task management with dependencies and validation |\n"
    )
    _git(repo, "add", "docs/guides/README.md")
    _git(repo, "commit", "-m", "add guides index")
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")

    integration_readme = (
        "# Gobby Guides\n\n"
        "Task-focused documentation for Gobby users, operators, and contributors.\n\n"
        "## Core Features\n\n"
        "| Guide | Description |\n"
        "|-------|-------------|\n"
        "| [search.md](./search.md) | Search across memories, tasks, and code content |\n"
        "| [tasks.md](./tasks.md) | Task management with dependencies, expansion, and git sync |\n"
        "\n_Last verified: 2026-05-06_\n"
    )
    (integration_path / "docs" / "guides" / "README.md").write_text(integration_readme)
    _git(integration_path, "add", "docs/guides/README.md")
    _git(integration_path, "commit", "-m", "refresh guide index structure")

    task_readme = (
        "# Gobby Guides\n\n"
        "Documentation guides for using Gobby's features.\n\n"
        "## Core Features\n\n"
        "| Guide | Description |\n"
        "|-------|-------------|\n"
        "| [search.md](search.md) | Domain search for tasks, memories, skills, and MCP tools |\n"
        "| [tasks.md](tasks.md) | Task management with dependencies and validation |\n"
    )
    (task_path / "docs" / "guides" / "README.md").write_text(task_readme)
    (task_path / "docs" / "guides" / "search.md").write_text("search\n")
    _git(task_path, "add", "docs/guides/README.md", "docs/guides/search.md")
    _git(task_path, "commit", "-m", "refresh search guide")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="docs",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    expected_readme = integration_readme.replace(
        "Search across memories, tasks, and code content",
        "Domain search for tasks, memories, skills, and MCP tools",
    )
    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert (integration_path / "docs" / "guides" / "README.md").read_text() == expected_readme
    assert (integration_path / "docs" / "guides" / "search.md").read_text() == "search\n"
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None


async def test_execute_merge_workspace_resolves_represented_docs_guides_readme_quick_link(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    integration_path = tmp_path / "integration"
    task_path = tmp_path / "task"
    repo.mkdir()
    _init_repo(repo)
    readme = repo / "docs" / "guides" / "README.md"
    readme.parent.mkdir(parents=True)
    base_readme = (
        "# Gobby Guides\n\n"
        "Documentation guides for using Gobby's features.\n\n"
        "## Quick Links\n\n"
        '- **Create a task**: `gobby tasks create "Title"` or `create_task` MCP tool\n'
        "- **Session handoff**: `gobby sessions summarize` or `create_handoff` MCP tool\n"
    )
    readme.write_text(base_readme)
    _git(repo, "add", "docs/guides/README.md")
    _git(repo, "commit", "-m", "add guides index")
    _git(repo, "worktree", "add", "-b", "integration/root", str(integration_path), "main")
    _git(repo, "worktree", "add", "-b", "task/leaf", str(task_path), "integration/root")
    _git(integration_path, "config", "user.email", "test@example.com")
    _git(integration_path, "config", "user.name", "Test User")
    _git(task_path, "config", "user.email", "test@example.com")
    _git(task_path, "config", "user.name", "Test User")

    integration_readme = (
        "# Gobby Guides\n\n"
        "Task-focused documentation for Gobby users, operators, and contributors.\n\n"
        "## Quick Links\n\n"
        '- **Create a task**: `gobby tasks create "Title"` or '
        '`create_task(title="Title", category="docs")`\n'
        "- **Session handoff**: `gobby sessions summarize` or "
        "`set_handoff` MCP tool\n"
        "\n_Last verified: 2026-05-06_\n"
    )
    (integration_path / "docs" / "guides" / "README.md").write_text(integration_readme)
    _git(integration_path, "add", "docs/guides/README.md")
    _git(integration_path, "commit", "-m", "refresh guide index quick links")

    task_readme = base_readme.replace("create_handoff", "set_handoff")
    (task_path / "docs" / "guides" / "README.md").write_text(task_readme)
    (task_path / "docs" / "guides" / "mcp-tools.md").write_text("mcp tools\n")
    _git(task_path, "add", "docs/guides/README.md", "docs/guides/mcp-tools.md")
    _git(task_path, "commit", "-m", "refresh mcp tools guide")

    project = _merge_checkout(temp_db, repo, monkeypatch)
    task_manager = LocalTaskManager(temp_db)
    parent = task_manager.create_task(
        project_id=project.id,
        title="Parent",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
    leaf = task_manager.create_task(
        project_id=project.id,
        title="Leaf",
        parent_task_id=parent.id,
        category="docs",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(leaf.id, stage_names=["merge"])
    task_manager.stage_states.start_stage(leaf.id, "merge", by_session_id="test")

    worktrees = LocalWorktreeManager(temp_db)
    worktrees.create(
        project_id=project.id,
        branch_name="integration/root",
        worktree_path=str(integration_path),
        base_branch="main",
        task_id=parent.id,
        workspace_role="integration",
    )
    source = worktrees.create(
        project_id=project.id,
        branch_name="task/leaf",
        worktree_path=str(task_path),
        base_branch="integration/root",
        task_id=leaf.id,
    )
    task_manager.artifacts.set_artifacts_atomic(
        leaf.id,
        worktree_path=str(task_path),
        worktree_id=source.id,
        base_commit_sha=_git(repo, "rev-parse", "main"),
        target_branch="integration/root",
    )

    merge_sha = await execute_merge_workspace(
        MergeWorkspaceAction(
            task_id=leaf.id,
            task_ref=f"#{leaf.seq_num}",
            backend="worktree",
            target_branch="integration/root",
            source_workspace_id=source.id,
        ),
        db=temp_db,
    )

    assert merge_sha == _git(integration_path, "rev-parse", "HEAD")
    assert (integration_path / "docs" / "guides" / "README.md").read_text() == integration_readme
    assert (integration_path / "docs" / "guides" / "mcp-tools.md").read_text() == "mcp tools\n"
    assert task_manager.stage_states.get(leaf.id, "merge").state == "done"
    _assert_worktree_removed(worktrees, source.id, task_path)
    assert task_manager.artifacts.get_artifacts(leaf.id).worktree_id is None


async def test_workspace_merge_uses_machine_checkout(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.build.workspaces import _project_repo_path
    from gobby.dispatch.workspace_merge import _repo_path_for_task
    from tests.fixtures.isolated_checkout import install_isolated_checkout_project

    repo = tmp_path / "merge-checkout"
    repo.mkdir()
    _init_repo(repo)
    isolated = install_isolated_checkout_project(
        temp_db, repo, name="merge-checkout", monkeypatch=monkeypatch
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=isolated.project.id,
        title="Merge checkout",
        task_type="task",
        category="code",
        validation_criteria="Test task completion is observable.",
    )

    assert _repo_path_for_task(temp_db, task.id) == Path(isolated.root_path)
    assert _project_repo_path(temp_db, isolated.project.id) == Path(isolated.root_path)


async def test_workspace_merge_fails_closed_without_checkout(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.build.workspaces import BuildWorkspaceError, _project_repo_path
    from gobby.dispatch.workspace_merge import _repo_path_for_task
    from gobby.storage.project_checkouts import CheckoutNotFoundError
    from tests.fixtures.isolated_checkout import insert_isolated_machine, patch_local_machine_id

    machine_id = insert_isolated_machine(temp_db)
    patch_local_machine_id(monkeypatch, machine_id)
    project = LocalProjectManager(temp_db).create("merge-missing-checkout")
    task = LocalTaskManager(temp_db).create_task(
        project_id=project.id,
        title="Merge missing checkout",
        task_type="task",
        category="code",
        validation_criteria="Test task completion is observable.",
    )

    with pytest.raises(CheckoutNotFoundError):
        _repo_path_for_task(temp_db, task.id)
    with pytest.raises((CheckoutNotFoundError, BuildWorkspaceError)):
        _project_repo_path(temp_db, project.id)
