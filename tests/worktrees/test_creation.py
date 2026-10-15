"""Creation must persist the same root used by native overlay identities."""

import subprocess
from pathlib import Path

import pytest
from psycopg import sql

from gobby.code_index.eligibility import code_index_id_for_root
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.managed_credential_types import resolve_auth_schema
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from gobby.worktrees.creation import create_worktree
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def local_base_repo(
    temp_db: PostgresHubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, LocalWorktreeManager, str]:
    root = tmp_path / "repo"
    isolated = install_isolated_checkout_project(
        temp_db, root, machine_id=require_machine_id(), monkeypatch=monkeypatch
    )
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "commit", "--allow-empty", "-m", "default base")
    _git(root, "tag", "seat-base")
    _git(root, "checkout", "-b", "seat-base")
    _git(root, "commit", "--allow-empty", "-m", "local base progress")
    return root, LocalWorktreeManager(temp_db), isolated.project.id


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("base_branch", ["seat-base", "refs/heads/seat-base"])
async def test_creation_uses_exact_local_branch_tip(
    local_base_repo: tuple[Path, LocalWorktreeManager, str], tmp_path: Path, base_branch: str
) -> None:
    root, storage, project_id = local_base_repo
    expected = _git(root, "rev-parse", "refs/heads/seat-base")
    assert expected != _git(root, "rev-parse", "main")
    result = await create_worktree(
        git_manager=WorktreeGitManager(root),
        worktree_storage=storage,
        project_id=project_id,
        branch_name="new-seat",
        base_branch=base_branch,
        worktree_path=str(tmp_path / "worktree"),
        event_emitter=lambda *_args, **_kwargs: {},
    )
    assert result.success, result.error
    assert result.worktree is not None
    assert _git(Path(result.worktree.worktree_path), "rev-parse", "HEAD") == expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_creation_refuses_stale_destination_branch(
    local_base_repo: tuple[Path, LocalWorktreeManager, str], tmp_path: Path
) -> None:
    root, storage, project_id = local_base_repo
    _git(root, "branch", "new-seat", "main")
    stale_tip = _git(root, "rev-parse", "refs/heads/new-seat")
    result = await create_worktree(
        git_manager=WorktreeGitManager(root),
        worktree_storage=storage,
        project_id=project_id,
        branch_name="new-seat",
        base_branch="seat-base",
        worktree_path=str(tmp_path / "worktree"),
        event_emitter=lambda *_args, **_kwargs: {},
    )
    assert not result.success
    assert result.error == "existing_branch_commit_mismatch"
    assert not (tmp_path / "worktree").exists()
    assert storage.get_by_branch(project_id, "new-seat") is None
    assert _git(root, "rev-parse", "refs/heads/new-seat") == stale_tip


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("base_branch", ["missing-base", "tag-only-base", "seat-base~1"])
async def test_creation_refuses_missing_local_branch(
    local_base_repo: tuple[Path, LocalWorktreeManager, str], tmp_path: Path, base_branch: str
) -> None:
    root, storage, project_id = local_base_repo
    _git(root, "tag", "tag-only-base", "main")
    result = await create_worktree(
        git_manager=WorktreeGitManager(root),
        worktree_storage=storage,
        project_id=project_id,
        branch_name="new-seat",
        base_branch=base_branch,
        worktree_path=str(tmp_path / "worktree"),
        event_emitter=lambda *_args, **_kwargs: {},
    )
    assert not result.success
    assert result.error is not None
    assert base_branch in result.error
    assert not (tmp_path / "worktree").exists()
    assert storage.get_by_branch(project_id, "new-seat") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_creation_registers_canonical_root_for_overlay_grants(
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
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    requested = alias / "worktree"
    storage = LocalWorktreeManager(temp_db)
    result = await create_worktree(
        git_manager=WorktreeGitManager(root),
        worktree_storage=storage,
        project_id=isolated.project.id,
        branch_name="ask-caller",
        base_branch="main",
        worktree_path=str(requested),
        event_emitter=lambda *_args, **_kwargs: {},
    )
    assert result.success, result.error
    assert result.worktree is not None
    stored = storage.get(result.worktree.id)
    assert stored is not None
    assert stored.worktree_path == str(requested.resolve())
    row = temp_db.fetchone(
        sql.SQL("SELECT {}.code_index_project_id(%s) AS overlay_id")
        .format(sql.Identifier(resolve_auth_schema(temp_db)))
        .as_string(),
        (stored.worktree_path,),
    )
    assert row is not None
    assert str(row["overlay_id"]) == code_index_id_for_root(requested)
