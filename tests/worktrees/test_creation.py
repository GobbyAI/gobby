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
        use_local=True,
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
