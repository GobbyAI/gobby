"""Worktree rows keep a branch name in base_branch, never a commit sha."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.worktrees._merge_state import is_worktree_git_merged
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import require_machine_id
from gobby.worktrees.base_branch import name_unreferenced_sha_base_rows
from gobby.worktrees.creation import WorktreeCreationResult, create_worktree
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

_OBSERVED_SHA = "246f7ed62e"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "moving", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
        capture_output=True,
    )
    _commit(root, "base")


def _row(base_branch: str, row_id: str) -> Worktree:
    now = datetime.now(UTC)
    return Worktree(
        id=row_id,
        project_id="project",
        machine_id="machine",
        task_id=None,
        branch_name="feature",
        worktree_path=f"/worktrees/{row_id}",
        base_branch=base_branch,
        agent_session_id=None,
        status="active",
        created_at=now,
        updated_at=now,
    )


async def _create(
    *,
    root: Path,
    storage: LocalWorktreeManager,
    project_id: str,
    branch_name: str,
    base_branch: str,
    worktree_path: Path,
) -> WorktreeCreationResult:
    return await create_worktree(
        git_manager=WorktreeGitManager(root),
        worktree_storage=storage,
        project_id=project_id,
        branch_name=branch_name,
        base_branch=base_branch,
        worktree_path=str(worktree_path),
        use_local=True,
        event_emitter=lambda *_args, **_kwargs: {},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_worktree_stores_base_branch_name_not_sha(
    temp_db: PostgresHubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    isolated = install_isolated_checkout_project(
        temp_db,
        root,
        machine_id=require_machine_id(),
        monkeypatch=monkeypatch,
    )
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    _init_repo(root)
    short = _git(root, "rev-parse", "--short=10", "moving")
    full = _git(root, "rev-parse", "moving")
    storage = LocalWorktreeManager(temp_db)

    created = await _create(
        root=root,
        storage=storage,
        project_id=isolated.project.id,
        branch_name="feature",
        base_branch="moving",
        worktree_path=tmp_path / "feature",
    )
    assert created.success, created.error
    assert created.worktree is not None
    stored = storage.get(created.worktree.id)
    assert stored is not None
    assert stored.base_branch == "moving"
    assert stored.base_branch != short
    assert stored.base_branch != full

    _commit(root, "base moves")
    reread = storage.get(stored.id)
    assert reread is not None
    assert reread.base_branch == "moving"

    sha_created = await _create(
        root=root,
        storage=storage,
        project_id=isolated.project.id,
        branch_name="from-sha",
        base_branch=short,
        worktree_path=tmp_path / "from-sha",
    )
    assert sha_created.success is False
    assert sha_created.error_code == "base_branch_is_commit_sha"
    assert storage.get_by_branch(isolated.project.id, "from-sha") is None

    refs = set(_git(root, "for-each-ref", "--format=%(refname:short)").splitlines())
    named = name_unreferenced_sha_base_rows(
        [
            _row(_OBSERVED_SHA, "observed"),
            _row(short, "short"),
            _row(full, "full"),
            _row("moving", "branch"),
            _row("a" * 9, "too-short"),
            _row("ab" * 21, "too-long"),
        ],
        refs,
    )
    referenced = name_unreferenced_sha_base_rows([_row(short, "referenced-sha")], refs | {short})
    assert {row.id for row in named} == {"observed", "short", "full"}
    assert referenced == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merged_worktree_against_branch_base_is_cleanup_eligible(
    temp_db: PostgresHubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    isolated = install_isolated_checkout_project(
        temp_db,
        root,
        machine_id=require_machine_id(),
        monkeypatch=monkeypatch,
    )
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    _init_repo(root)
    storage = LocalWorktreeManager(temp_db)
    manager = WorktreeGitManager(root)
    created = await _create(
        root=root,
        storage=storage,
        project_id=isolated.project.id,
        branch_name="feature",
        base_branch="moving",
        worktree_path=tmp_path / "feature",
    )
    assert created.success, created.error
    assert created.worktree is not None
    feature = Path(created.worktree.worktree_path)
    (feature / "change.txt").write_text("change\n", encoding="utf-8")
    _git(feature, "add", "change.txt")
    _commit(feature, "feature work")
    _git(root, "merge", "--no-ff", "feature", "-m", "merge feature")

    stored = storage.get(created.worktree.id)
    assert stored is not None
    assert stored.base_branch == "moving"
    assert await is_worktree_git_merged(stored, manager) is True

    merged = storage.mark_merged(stored.id)
    assert merged is not None
    assert merged.status == "merged"
    assert merged.cleanup_after is not None
    storage.update(stored.id, cleanup_after=utc_now() - timedelta(seconds=5))
    expired = storage.find_expired(project_id=isolated.project.id)
    assert stored.id in {row.id for row in expired}

    refs = set(_git(root, "for-each-ref", "--format=%(refname:short)").splitlines())
    named = name_unreferenced_sha_base_rows([_row(_OBSERVED_SHA, "observed")], refs)
    assert [row.id for row in named] == ["observed"]
    assert [row.base_branch for row in named] == [_OBSERVED_SHA]
