"""Tests for merge_worktree tool in _sync.py — worktree_path returns and auto-resolve."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _make_git_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Create a mock git subprocess result."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# Stash sequence: stash list (before), stash push, stash list (after), ... , stash pop
_STASH_BEFORE = [
    _make_git_result(0, stdout=""),  # stash list (before)
    _make_git_result(0),  # stash push
    _make_git_result(0, stdout="stash@{0}"),  # stash list (after) — different = stash created
]
_STASH_POP = [_make_git_result(0)]  # stash pop
_MERGE_BASE_SUCCESS = [_make_git_result(0)]  # merge-base --is-ancestor
_LOCAL_TARGET_EXISTS = [
    _make_git_result(0),  # show-ref refs/heads/<target>
    _make_git_result(1),  # show-ref refs/remotes/origin/<target>
]
_LOCAL_AND_REMOTE_TARGET_EXIST = [
    _make_git_result(0),  # show-ref refs/heads/<target>
    _make_git_result(0),  # show-ref refs/remotes/origin/<target>
]


def _make_registry_context(
    worktree_path: str = "/tmp/wt", branch: str = "feat", base: str = "main"
):
    """Create a mock RegistryContext with worktree storage and git manager."""
    ctx = MagicMock()
    wt = MagicMock()
    wt.worktree_path = worktree_path
    wt.branch_name = branch
    wt.base_branch = base
    ctx.worktree_storage.get.return_value = wt
    ctx.git_manager = MagicMock()
    ctx.git_manager.run_git_command.side_effect = (
        lambda args, cwd=None, timeout=30, check=False: ctx.git_manager._run_git(
            args, cwd=cwd, timeout=timeout, check=check
        )
    )

    def get_unmerged_files(cwd=None):
        result = ctx.git_manager._run_git(
            ["diff", "--name-only", "--diff-filter=U"], cwd=cwd, timeout=10
        )
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

    ctx.git_manager.get_unmerged_files.side_effect = get_unmerged_files
    ctx.project_id = "test-project"
    return ctx


@pytest.mark.asyncio
async def test_merge_worktree_success_returns_worktree_path():
    """Successful merge returns worktree_path."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = [
        _make_git_result(0),  # fetch
        *_LOCAL_TARGET_EXISTS,
        *_STASH_BEFORE,
        _make_git_result(0),  # merge succeeds
        *_MERGE_BASE_SUCCESS,
        *_STASH_POP,
    ]

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert result["worktree_path"] == "/tmp/wt"
    assert result["merged"] is True


@pytest.mark.asyncio
async def test_merge_worktree_local_only_target_uses_local_branch():
    """Local target branch is merged directly when no remote target exists."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context(base="develop")

    ctx.git_manager._run_git.side_effect = [
        _make_git_result(0),  # fetch
        *_LOCAL_TARGET_EXISTS,
        *_STASH_BEFORE,
        _make_git_result(0),  # merge succeeds
        *_MERGE_BASE_SUCCESS,
        *_STASH_POP,
    ]

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    merge_calls = [
        call
        for call in ctx.git_manager._run_git.call_args_list
        if call[0][0][0] == "merge" and "--no-edit" in call[0][0]
    ]
    assert len(merge_calls) == 1
    assert merge_calls[0][0][0] == ["merge", "develop", "--no-edit"]


@pytest.mark.asyncio
async def test_merge_worktree_prefer_remote_uses_remote_ref():
    """Remote target selection is explicit."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context(base="develop")

    ctx.git_manager._run_git.side_effect = [
        _make_git_result(0),  # fetch
        *_LOCAL_AND_REMOTE_TARGET_EXIST,
        *_STASH_BEFORE,
        _make_git_result(0),  # merge succeeds
        *_MERGE_BASE_SUCCESS,
        *_STASH_POP,
    ]

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123", prefer_remote=True)

    assert result["success"] is True
    merge_calls = [
        call
        for call in ctx.git_manager._run_git.call_args_list
        if call[0][0][0] == "merge" and "--no-edit" in call[0][0]
    ]
    assert len(merge_calls) == 1
    assert merge_calls[0][0][0] == ["merge", "origin/develop", "--no-edit"]


@pytest.mark.asyncio
async def test_merge_worktree_conflict_returns_worktree_path():
    """Merge with conflicts returns worktree_path."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = [
        _make_git_result(0),  # fetch
        *_LOCAL_TARGET_EXISTS,
        *_STASH_BEFORE,
        _make_git_result(1, stderr="CONFLICT"),  # merge fails
        _make_git_result(0, stdout="src/main.py\n"),  # diff --name-only
        _make_git_result(0),  # merge --abort
        *_STASH_POP,
    ]

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with (
        patch(
            "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
            return_value=(ctx.git_manager, "test-project", None),
        ),
        patch(
            "gobby.worktrees.merge.resolver.auto_resolve_trivial_conflicts",
            new_callable=AsyncMock,
            return_value=["src/main.py"],
        ),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is False
    assert result["has_conflicts"] is True
    assert result["merged"] is False
    assert result["worktree_path"] == "/tmp/wt"


@pytest.mark.asyncio
async def test_merge_worktree_auto_resolves_trivial_conflicts():
    """Merge auto-resolves .gobby/*.jsonl and succeeds when no real conflicts remain."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = [
        _make_git_result(0),  # fetch
        *_LOCAL_TARGET_EXISTS,
        *_STASH_BEFORE,
        _make_git_result(1, stderr="CONFLICT"),  # merge fails
        _make_git_result(0, stdout=".gobby/tasks.jsonl\n"),  # diff --name-only
        _make_git_result(0),  # commit --no-edit
        *_MERGE_BASE_SUCCESS,
        *_STASH_POP,
    ]

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with (
        patch(
            "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
            return_value=(ctx.git_manager, "test-project", None),
        ),
        patch(
            "gobby.worktrees.merge.resolver.auto_resolve_trivial_conflicts",
            new_callable=AsyncMock,
            return_value=[],  # all trivial, nothing remaining
        ),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert "auto-resolved" in result["message"]
    assert result["worktree_path"] == "/tmp/wt"
    assert result["auto_resolved"] == [".gobby/tasks.jsonl"]
    assert result["merged"] is True


@pytest.mark.asyncio
async def test_merge_worktree_push_failure_returns_worktree_path():
    """Push failure returns worktree_path."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = [
        _make_git_result(0),  # fetch
        *_LOCAL_TARGET_EXISTS,
        *_STASH_BEFORE,
        _make_git_result(0),  # merge succeeds
        _make_git_result(1, stderr="rejected"),  # push fails
        *_STASH_POP,
    ]

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123", push=True)

    assert result["success"] is False
    assert result["worktree_path"] == "/tmp/wt"
    assert result["merge_succeeded"] is True


@pytest.mark.asyncio
async def test_merge_worktree_non_conflict_error_returns_worktree_path():
    """Non-conflict merge error returns worktree_path."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = [
        _make_git_result(0),  # fetch
        *_LOCAL_TARGET_EXISTS,
        *_STASH_BEFORE,
        _make_git_result(128, stdout="fatal: not a git repo", stderr=""),  # merge fails
        _make_git_result(0, stdout=""),  # diff --name-only (no conflicts)
        *_STASH_POP,
    ]

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is False
    assert result["has_conflicts"] is False
    assert result["worktree_path"] == "/tmp/wt"


@pytest.mark.asyncio
async def test_merge_worktree_stash_restores_on_success():
    """Stash pop is called after successful merge."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = [
        _make_git_result(0),  # fetch
        *_LOCAL_TARGET_EXISTS,
        *_STASH_BEFORE,
        _make_git_result(0),  # merge succeeds
        *_MERGE_BASE_SUCCESS,
        *_STASH_POP,
    ]

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        await merge_tool("wt-123")

    # Last call should be stash pop
    last_call_args = ctx.git_manager._run_git.call_args_list[-1]
    assert last_call_args[0][0] == ["stash", "pop"]
