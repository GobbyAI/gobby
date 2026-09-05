"""Real Git coverage for deleting landed worktrees after their base disappears."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.storage.worktrees import Worktree
from gobby.worktrees.executor import WorktreeDeleteExecutor
from gobby.worktrees.git import WorktreeGitManager

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


@pytest.fixture
def landed_worktree(tmp_path: Path) -> tuple[Path, Worktree]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    _git(repo, "switch", "-c", "0.5.0")
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    _git(repo, "worktree", "add", "-b", "parent", str(parent))
    _git(parent, "commit", "--allow-empty", "-m", "parent work")
    _git(repo, "worktree", "add", "-b", "child", str(child), "parent")
    _git(child, "commit", "--allow-empty", "-m", "child work")
    _git(repo, "merge", "--no-ff", "child", "-m", "land child")
    _git(repo, "tag", "landed")
    _git(repo, "update-ref", "refs/remotes/origin/0.5.0", "HEAD")
    _git(repo, "symbolic-ref", "refs/heads/child-alias", "refs/heads/child")
    removed = WorktreeGitManager(repo).delete_worktree(
        parent, delete_branch=True, branch_name="parent", base_branch="0.5.0"
    )
    assert removed.success, removed.message
    now = datetime.now(UTC)
    return repo, Worktree(
        id="worktree-child",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="child",
        worktree_path=str(child),
        base_branch="parent",
        status="merged",
        created_at=now,
        updated_at=now,
        task_id=None,
        agent_session_id=None,
    )


@pytest.fixture
def deletion_registry(
    landed_worktree: tuple[Path, Worktree],
) -> Iterator[tuple[InternalToolRegistry, MagicMock]]:
    repo, worktree = landed_worktree
    storage = MagicMock()
    storage.resolve_reference.side_effect = lambda ref: ref
    storage.get.return_value = worktree
    storage.delete.return_value = True
    executor = WorktreeDeleteExecutor(thread_name_prefix="test-explicit-merge-target")
    try:
        with patch("gobby.worktrees.deletion.emit_worktree_event"):
            yield (
                create_worktrees_registry(
                    worktree_storage=storage,
                    git_manager=WorktreeGitManager(repo),
                    project_id=worktree.project_id,
                    worktree_delete_executor=executor,
                ),
                storage,
            )
    finally:
        executor.shutdown()
        executor.join()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["0.5.0", "refs/heads/0.5.0"])
async def test_delete_with_explicit_landing_target_after_parent_cleanup(
    landed_worktree: tuple[Path, Worktree],
    deletion_registry: tuple[InternalToolRegistry, MagicMock],
    target: str,
) -> None:
    repo, worktree = landed_worktree
    registry, storage = deletion_registry
    delete = registry.get_tool("delete_worktree")
    assert delete is not None
    refused = await delete(worktree_id=worktree.id)
    assert refused["success"] is False
    assert Path(worktree.worktree_path).exists()
    storage.delete.assert_not_called()

    result = await delete(worktree_id=worktree.id, merged_into=target)

    assert result["success"] is True
    assert not Path(worktree.worktree_path).exists()
    assert _git(repo, "branch", "--list", "child", "parent") == ""
    assert "child work" in _git(repo, "log", "0.5.0", "--format=%s")
    storage.delete.assert_called_once_with(worktree.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "main",
        "missing",
        "child",
        "refs/heads/child",
        "origin/0.5.0",
        "refs/remotes/origin/0.5.0",
        "refs/tags/landed",
        "0.5.0~0",
        "child-alias",
        "",
    ],
)
async def test_explicit_target_refusal_preserves_worktree_and_record(
    landed_worktree: tuple[Path, Worktree],
    deletion_registry: tuple[InternalToolRegistry, MagicMock],
    target: str,
) -> None:
    repo, worktree = landed_worktree
    registry, storage = deletion_registry
    original_tip = _git(repo, "rev-parse", "refs/heads/child")
    delete = registry.get_tool("delete_worktree")
    assert delete is not None

    result = await delete(worktree_id=worktree.id, merged_into=target)

    assert result["success"] is False
    assert result["error"]
    assert result["error_code"]
    if target == "missing":
        assert result["error_code"] == "merge_target_unresolvable"
        assert "Set merged_into to an existing local branch" in result["error"]
    assert Path(worktree.worktree_path).exists()
    assert _git(repo, "rev-parse", "refs/heads/child") == original_tip
    storage.delete.assert_not_called()


def test_delete_schema_exposes_explicit_local_merge_target(
    deletion_registry: tuple[InternalToolRegistry, MagicMock],
) -> None:
    registry, _ = deletion_registry
    schema = registry.get_schema("delete_worktree")
    assert schema is not None
    assert schema["inputSchema"]["properties"]["merged_into"]["type"] == "string"


@pytest.mark.asyncio
async def test_explicit_target_cannot_delete_a_detached_worktree(
    landed_worktree: tuple[Path, Worktree],
    deletion_registry: tuple[InternalToolRegistry, MagicMock],
) -> None:
    _, worktree = landed_worktree
    registry, storage = deletion_registry
    _git(Path(worktree.worktree_path), "checkout", "--detach")
    worktree.branch_name = None
    delete = registry.get_tool("delete_worktree")
    assert delete is not None

    result = await delete(worktree_id=worktree.id, merged_into="0.5.0")

    assert result["success"] is False
    assert result["error_code"] == "merged_into_requires_source_branch"
    assert Path(worktree.worktree_path).exists()
    storage.delete.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_target_requires_git_context(
    landed_worktree: tuple[Path, Worktree],
) -> None:
    _, worktree = landed_worktree
    storage = MagicMock()
    storage.resolve_reference.side_effect = lambda ref: ref
    storage.get.return_value = worktree
    executor = WorktreeDeleteExecutor(thread_name_prefix="test-missing-merge-context")
    try:
        registry = create_worktrees_registry(
            worktree_storage=storage,
            git_manager=None,
            project_id=worktree.project_id,
            worktree_delete_executor=executor,
        )
        result = await registry.call(
            "delete_worktree", {"worktree_id": worktree.id, "merged_into": "0.5.0"}
        )
    finally:
        executor.shutdown()
        executor.join()

    assert result["success"] is False
    assert result["error_code"] == "merged_into_requires_git_manager"
    assert Path(worktree.worktree_path).exists()
    storage.delete.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_target_refuses_new_commits_after_landing(
    landed_worktree: tuple[Path, Worktree],
    deletion_registry: tuple[InternalToolRegistry, MagicMock],
) -> None:
    repo, worktree = landed_worktree
    registry, storage = deletion_registry
    _git(Path(worktree.worktree_path), "commit", "--allow-empty", "-m", "not landed")
    delete = registry.get_tool("delete_worktree")
    assert delete is not None

    result = await delete(worktree_id=worktree.id, merged_into="0.5.0")

    assert result["success"] is False
    assert Path(worktree.worktree_path).exists()
    assert _git(repo, "log", "child", "-1", "--format=%s") == "not landed"
    storage.delete.assert_not_called()


@pytest.mark.asyncio
async def test_missing_directory_does_not_hide_failed_explicit_merge_proof(
    landed_worktree: tuple[Path, Worktree],
    deletion_registry: tuple[InternalToolRegistry, MagicMock],
) -> None:
    repo, worktree = landed_worktree
    registry, storage = deletion_registry
    _git(repo, "worktree", "remove", worktree.worktree_path)
    delete = registry.get_tool("delete_worktree")
    assert delete is not None

    result = await delete(worktree_id=worktree.id, merged_into="main")

    assert result["success"] is False
    assert _git(repo, "branch", "--list", "child") == "child"
    storage.delete.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_merge_proof_cannot_be_combined_with_branch_force(
    landed_worktree: tuple[Path, Worktree],
    deletion_registry: tuple[InternalToolRegistry, MagicMock],
) -> None:
    _, worktree = landed_worktree
    registry, storage = deletion_registry
    delete = registry.get_tool("delete_worktree")
    assert delete is not None

    result = await delete(worktree_id=worktree.id, merged_into="main", force_delete_branch=True)

    assert result["success"] is False
    assert Path(worktree.worktree_path).exists()
    storage.delete.assert_not_called()
