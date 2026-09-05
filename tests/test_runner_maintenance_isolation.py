"""Tests for runner isolation maintenance cleanup."""

from __future__ import annotations

import asyncio
import subprocess
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.runner_maintenance import (
    _cleanup_missing_isolation_records,
    cleanup_expired_isolation_loop,
)
from gobby.runner_maintenance.isolation import _cleanup_missing_isolation_records_async
from gobby.storage.clones import Clone, LocalCloneManager
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import Project
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.worktrees.executor import WorktreeDeleteExecutor
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["landed", "unmerged", "dirty", "claimed_task"])
async def test_expiry_preserves_active_or_unlanded_work(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    protection: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    path = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-b", "task/expired", str(path))
    _git(path, "commit", "--allow-empty", "-m", "landed work")
    _git(repo, "merge", "--no-ff", "task/expired", "-m", "land work")
    project = _install_project(temp_db, repo, monkeypatch)
    storage = LocalWorktreeManager(temp_db)
    task_id = None
    if protection == "claimed_task":
        session = SessionManager(temp_db).register(
            external_id="active-expiry-owner",
            source="codex",
            machine_id=None,
            project_id=project.id,
        )
        task = LocalTaskManager(temp_db).create_task(
            project.id, "Active epic", task_type="epic", claimed_by_session_id=session.id
        )
        task_id = task.id
    worktree = storage.create(project.id, "task/expired", str(path), task_id=task_id)
    storage.mark_merged(worktree.id)
    if protection == "unmerged":
        _git(path, "commit", "--allow-empty", "-m", "new unlanded work")
    elif protection == "dirty":
        (path / "uncommitted.txt").write_text("must survive\n")
    source_tip = _git(repo, "rev-parse", "refs/heads/task/expired")
    shutdown = iter([False, True])
    caplog.set_level("WARNING")

    await cleanup_expired_isolation_loop(temp_db, lambda: next(shutdown), interval_hours=0)

    if protection == "landed":
        assert not path.exists()
        assert storage.get(worktree.id) is None
        assert _git(repo, "branch", "--list", "task/expired") == ""
    else:
        assert path.exists()
        assert storage.get(worktree.id) is not None
        assert _git(repo, "rev-parse", "refs/heads/task/expired") == source_tip
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protection", ["landed", "unmerged", "untracked", "staged", "claimed_task"]
)
async def test_clone_expiry_requires_clean_head_in_parent_repo(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    protection: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    clone_root = tmp_path / "clones"
    clone_root.mkdir()
    path = clone_root / "expired"
    monkeypatch.setattr("gobby.clones.git.CLONES_ROOT", clone_root)
    _git(repo, "clone", "--no-hardlinks", str(repo), str(path))
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "switch", "-c", "task/clone")
    _git(path, "commit", "--allow-empty", "-m", "landed clone work")
    _git(repo, "fetch", str(path), "task/clone")
    _git(repo, "merge", "--no-ff", "FETCH_HEAD", "-m", "land clone")
    project = _install_project(temp_db, repo, monkeypatch)
    task_id = None
    if protection == "claimed_task":
        session = SessionManager(temp_db).register(
            external_id="active-clone-owner", source="codex", machine_id=None, project_id=project.id
        )
        task_id = (
            LocalTaskManager(temp_db)
            .create_task(
                project.id,
                "Active clone task",
                claimed_by_session_id=session.id,
                validation_criteria="Active clone directory survives expiry",
            )
            .id
        )
    storage = LocalCloneManager(temp_db)
    clone = storage.create(project.id, "task/clone", str(path), task_id=task_id)
    storage.mark_merged(clone.id, cleanup_after=datetime(2020, 1, 1, tzinfo=UTC))
    if protection == "unmerged":
        _git(path, "commit", "--allow-empty", "-m", "new unlanded clone work")
    elif protection in ("untracked", "staged"):
        (path / "new.txt").write_text("must survive\n")
        if protection == "staged":
            _git(path, "add", "new.txt")
    source_tip = _git(path, "rev-parse", "HEAD")
    shutdown = iter([False, True])
    caplog.set_level("WARNING")

    await cleanup_expired_isolation_loop(temp_db, lambda: next(shutdown), interval_hours=0)

    if protection == "landed":
        assert not path.exists()
        assert storage.get(clone.id) is None
    else:
        assert path.exists()
        assert storage.get(clone.id) is not None
        assert _git(path, "rev-parse", "HEAD") == source_tip
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["worktree", "clone"])
@pytest.mark.parametrize("owner", ["open_task", "claimed_task", "claimed_workspace", "closed_task"])
async def test_missing_records_preserve_current_owners(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    owner: str,
) -> None:
    project = _install_project(temp_db, tmp_path / "repo", monkeypatch)
    session = SessionManager(temp_db).register(
        external_id=f"missing-{kind}-{owner}",
        source="codex",
        machine_id=None,
        project_id=project.id,
    )
    task = LocalTaskManager(temp_db).create_task(
        project.id,
        "Missing workspace owner",
        claimed_by_session_id=session.id if owner == "claimed_task" else None,
        validation_criteria="Missing owned workspace metadata survives cleanup",
    )
    if owner in ("closed_task", "claimed_workspace"):
        temp_db.execute("UPDATE tasks SET closed_at = NOW() WHERE id = %s", (task.id,))
    worktrees = LocalWorktreeManager(temp_db)
    clones = LocalCloneManager(temp_db)
    storage = worktrees if kind == "worktree" else clones
    workspace = storage.create(
        project.id, "task/missing", str(tmp_path / "missing"), task_id=task.id
    )
    storage.update(workspace.id, status="merged", cleanup_after=datetime(2020, 1, 1, tzinfo=UTC))
    if owner == "claimed_workspace":
        storage.claim(workspace.id, session.id)

    shutdown = iter([False, True])
    await cleanup_expired_isolation_loop(temp_db, lambda: next(shutdown), interval_hours=0)

    assert (storage.get(workspace.id) is None) == (owner == "closed_task")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["worktree", "clone"])
@pytest.mark.parametrize("after_mutation", [False, True])
async def test_missing_cleanup_cancellation_stops_before_next_record(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    after_mutation: bool,
) -> None:
    project = _install_project(temp_db, tmp_path / "repo", monkeypatch)
    worktrees = LocalWorktreeManager(temp_db)
    clones = LocalCloneManager(temp_db)
    storage = worktrees if kind == "worktree" else clones
    first = storage.create(project.id, "task/first", str(tmp_path / "first"))
    second = storage.create(project.id, "task/second", str(tmp_path / "second"))
    monkeypatch.setattr(
        storage,
        "list_worktrees" if kind == "worktree" else "list_clones",
        lambda *, limit: [first, second],
    )
    entered = threading.Event()
    release = threading.Event()
    lock_for_cleanup = storage.lock_for_cleanup
    delete = storage.delete

    @contextmanager
    def gated_lock(
        workspace_id: str, *, expired_only: bool = True
    ) -> Iterator[Worktree | Clone | None]:
        with lock_for_cleanup(workspace_id, expired_only=expired_only) as row:
            if workspace_id == first.id and not after_mutation:
                entered.set()
                assert release.wait(timeout=2)
            yield row

    def gated_delete(workspace_id: str) -> bool:
        if workspace_id == first.id and after_mutation:
            entered.set()
            assert release.wait(timeout=2)
        return delete(workspace_id)

    monkeypatch.setattr(storage, "lock_for_cleanup", gated_lock)
    monkeypatch.setattr(storage, "delete", gated_delete)
    executor = WorktreeDeleteExecutor(max_workers=1)
    sweep = asyncio.create_task(
        _cleanup_missing_isolation_records_async(
            worktrees,
            clones,
            run_db=None,
            worktree_delete_executor=executor,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        sweep.cancel()
        await asyncio.sleep(0)
        assert not sweep.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(sweep, timeout=2)
        assert (storage.get(first.id) is None) == after_mutation
        assert storage.get(second.id) is not None
        with lock_for_cleanup(second.id, expired_only=False) as row:
            assert row is not None
    finally:
        release.set()
        executor.shutdown()
        executor.join()


@pytest.mark.asyncio
@pytest.mark.parametrize("ref_state", ["unmerged", "absent", "lookup_failed"])
async def test_missing_worktree_requires_absent_source_ref(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ref_state: str,
) -> None:
    repo = tmp_path / "repo"
    project = _install_project(temp_db, repo, monkeypatch)
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    path = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-b", "task/missing", str(path))
    _git(path, "commit", "--allow-empty", "-m", "unlanded work")
    tip = _git(path, "rev-parse", "HEAD")
    _git(repo, "worktree", "remove", str(path))
    if ref_state != "unmerged":
        _git(repo, "branch", "-D", "task/missing")
    task = LocalTaskManager(temp_db).create_task(
        project.id,
        "Closed missing workspace task",
        validation_criteria="Missing source checkout does not erase surviving branch registration",
    )
    temp_db.execute("UPDATE tasks SET closed_at = NOW() WHERE id = %s", (task.id,))
    storage = LocalWorktreeManager(temp_db)
    workspace = storage.create(project.id, "task/missing", str(path), task_id=task.id)
    storage.update(workspace.id, status="merged", cleanup_after=datetime(2020, 1, 1, tzinfo=UTC))
    if ref_state == "lookup_failed":

        def failed_lookup(
            _manager: WorktreeGitManager,
            args: list[str],
            *,
            timeout: int,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 128, "", "ref database unavailable")

        monkeypatch.setattr(WorktreeGitManager, "run_git_command", failed_lookup)
    shutdown = iter([False, True])
    caplog.set_level("WARNING")

    await cleanup_expired_isolation_loop(temp_db, lambda: next(shutdown), interval_hours=0)

    assert not path.exists()
    assert (storage.get(workspace.id) is None) == (ref_state == "absent")
    if ref_state == "unmerged":
        assert _git(repo, "rev-parse", "refs/heads/task/missing") == tip
    assert caplog.records == []


def _install_project(
    temp_db: HubDatabase,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Project:
    isolated = install_isolated_checkout_project(
        temp_db,
        root,
        name="proj-1",
        monkeypatch=monkeypatch,
    )
    if not (root / ".git").exists():
        _git(root, "init", "-b", "main")
    monkeypatch.setattr(
        "gobby.storage.worktrees.require_machine_id",
        lambda: isolated.machine_id,
    )
    monkeypatch.setattr(
        "gobby.storage.clones.require_machine_id",
        lambda: isolated.machine_id,
    )
    return isolated.project


def test_cleanup_missing_isolation_records_removes_dead_paths(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worktree and clone records with missing directories are removed."""
    project = _install_project(temp_db, tmp_path / "repo", monkeypatch)
    worktrees = LocalWorktreeManager(temp_db)
    clones = LocalCloneManager(temp_db)

    existing_worktree_path = tmp_path / "existing-worktree"
    existing_worktree_path.mkdir()
    existing_clone_path = tmp_path / "existing-clone"
    existing_clone_path.mkdir()

    missing_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/missing-worktree",
        worktree_path=str(tmp_path / "missing-worktree"),
    )
    existing_worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/existing-worktree",
        worktree_path=str(existing_worktree_path),
    )
    missing_clone = clones.create(
        project_id=project.id,
        branch_name="task/missing-clone",
        clone_path=str(tmp_path / "missing-clone"),
    )
    existing_clone = clones.create(
        project_id=project.id,
        branch_name="task/existing-clone",
        clone_path=str(existing_clone_path),
    )

    counts = _cleanup_missing_isolation_records(worktrees, clones)

    assert counts == {"worktrees": 1, "clones": 1}
    assert worktrees.get(missing_worktree.id) is None
    assert clones.get(missing_clone.id) is None
    assert worktrees.get(existing_worktree.id) is not None
    assert clones.get(existing_clone.id) is not None


@pytest.mark.asyncio
async def test_expired_isolation_loop_uses_bounded_db_runner(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing-record cleanup in the periodic loop keeps PostgreSQL handles bounded."""
    project = _install_project(temp_db, tmp_path / "repo", monkeypatch)
    LocalWorktreeManager(temp_db).create(
        project_id=project.id,
        branch_name="task/missing-worktree",
        worktree_path=str(tmp_path / "missing-worktree"),
    )
    LocalCloneManager(temp_db).create(
        project_id=project.id,
        branch_name="task/missing-clone",
        clone_path=str(tmp_path / "missing-clone"),
    )

    executor = DatabaseExecutor(max_workers=2, thread_name_prefix="isolation-db")
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    try:
        await asyncio.wait_for(
            cleanup_expired_isolation_loop(
                temp_db,
                is_shutdown_requested,
                interval_hours=0,
                run_db=executor.run,
            ),
            timeout=2,
        )
        stats = executor.stats()
        assert stats.submitted == stats.completed
        assert stats.active == 0
        assert stats.queued == 0
        assert stats.threads <= executor.max_workers
    finally:
        executor.shutdown()
        executor.join()


@pytest.mark.asyncio
async def test_expired_isolation_loop_preserves_invalid_clones(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _install_project(temp_db, tmp_path / "repo", monkeypatch)
    clones = LocalCloneManager(temp_db)
    active_path = tmp_path / "expired-active-clone"
    active_path.mkdir()
    active = clones.create(
        project_id=project.id,
        branch_name="task/expired-active-clone",
        clone_path=str(active_path),
        cleanup_after=datetime(2020, 1, 1, tzinfo=UTC),
    )
    merged_path = tmp_path / "expired-merged-clone"
    merged_path.mkdir()
    merged = clones.create(
        project_id=project.id,
        branch_name="task/expired-merged-clone",
        clone_path=str(merged_path),
    )
    clones.mark_merged(merged.id, cleanup_after=datetime(2020, 1, 1, tzinfo=UTC))
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    await cleanup_expired_isolation_loop(
        temp_db,
        is_shutdown_requested,
        interval_hours=0,
    )

    assert active_path.exists()
    assert clones.get(active.id) is not None
    assert merged_path.exists()
    assert clones.get(merged.id) is not None


@pytest.mark.asyncio
async def test_expired_isolation_loop_runs_git_in_parent_repo(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired cleanup targets the recorded repo when daemon cwd is unrelated."""
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "expired-worktree"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True)
    (repo_path / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "task/expired", str(worktree_path)],
        cwd=repo_path,
        check=True,
    )

    project = _install_project(temp_db, repo_path, monkeypatch)
    worktrees = LocalWorktreeManager(temp_db)
    worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/expired",
        worktree_path=str(worktree_path),
    )
    worktrees.mark_merged(worktree.id)

    unrelated_path = tmp_path / "unrelated"
    unrelated_path.mkdir()
    monkeypatch.chdir(unrelated_path)
    run_git = WorktreeGitManager._run_git
    git_cwds: list[tuple[list[str], Path]] = []

    def record_git_cwd(
        manager: WorktreeGitManager,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 30,
        check: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        git_cwds.append((args, Path(cwd) if cwd is not None else manager.repo_path))
        return run_git(manager, args, cwd=cwd, timeout=timeout, check=check, env=env)

    monkeypatch.setattr(WorktreeGitManager, "_run_git", record_git_cwd)
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    await cleanup_expired_isolation_loop(
        temp_db,
        is_shutdown_requested,
        interval_hours=0,
    )

    worktree_list = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    branch_list = subprocess.run(
        ["git", "branch", "--list", "task/expired"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(worktree_path) not in worktree_list.stdout
    assert branch_list.stdout == ""
    assert worktrees.get(worktree.id) is None
    mutations = [
        (args, cwd)
        for args, cwd in git_cwds
        if args[:2] in (["worktree", "remove"], ["branch", "-D"])
    ]
    assert mutations == [
        (["worktree", "remove", str(worktree_path)], repo_path),
        (["branch", "-D", "task/expired"], repo_path),
    ]


@pytest.mark.asyncio
async def test_expired_isolation_loop_preserves_failed_git_cleanup(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused Git operation preserves the directory and row without force fallback."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.name", "Test User")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "commit", "--allow-empty", "-m", "initial")
    worktree_path = tmp_path / "expired-worktree"
    _git(repo_path, "worktree", "add", "-b", "task/expired", str(worktree_path))
    project = _install_project(temp_db, repo_path, monkeypatch)
    worktrees = LocalWorktreeManager(temp_db)
    worktree = worktrees.create(
        project_id=project.id,
        branch_name="task/expired",
        worktree_path=str(worktree_path),
    )
    worktrees.mark_merged(worktree.id)

    def refuse_removal(
        manager: WorktreeGitManager,
        args: list[str],
        cwd: str | Path | None = None,
        timeout: int = 30,
        check: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["worktree", "remove"]:
            return subprocess.CompletedProcess(args, 1, "", "worktree removal refused")
        return run_git(manager, args, cwd=cwd, timeout=timeout, check=check, env=env)

    run_git = WorktreeGitManager._run_git
    monkeypatch.setattr(WorktreeGitManager, "_run_git", refuse_removal)
    caplog.set_level("WARNING")
    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    await cleanup_expired_isolation_loop(
        temp_db,
        is_shutdown_requested,
        interval_hours=0,
    )

    assert worktree_path.exists()
    assert worktrees.get(worktree.id) is not None
    assert _git(repo_path, "branch", "--list", "task/expired") == "+ task/expired"
    assert caplog.records == []
