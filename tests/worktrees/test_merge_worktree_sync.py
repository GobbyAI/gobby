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
    ctx.git_manager.repo_path = "/tmp/repo"
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


def _local_merge_side_effect(
    *,
    source: str = "feat",
    target: str = "main",
    merge_result: MagicMock | None = None,
    unmerged_stdout: str = "",
    status_stdout: str = "",
    incoming_stdout: str = "feature.txt\n",
):
    stash_list_calls = 0

    def _run_git(args, cwd=None, timeout=30, check=False):
        nonlocal stash_list_calls
        if args == ["show-ref", "--verify", "--quiet", f"refs/heads/{target}"]:
            return _make_git_result(0)
        if args == ["show-ref", "--verify", "--quiet", f"refs/heads/{source}"]:
            return _make_git_result(0)
        if args == ["status", "--porcelain"]:
            return _make_git_result(0, stdout=status_stdout)
        if args == ["diff", "--name-only", "HEAD", source]:
            return _make_git_result(0, stdout=incoming_stdout)
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return _make_git_result(0, stdout=target)
        if args == ["rev-parse", "HEAD"]:
            return _make_git_result(0, stdout="abc123def456\n")
        if args == ["stash", "list"]:
            stash_list_calls += 1
            return _make_git_result(0, stdout="" if stash_list_calls == 1 else "stash@{0}")
        if args[:2] == ["stash", "push"]:
            return _make_git_result(0)
        if args == ["stash", "pop"]:
            return _make_git_result(0)
        if args == ["merge", source, "--no-edit"]:
            return merge_result or _make_git_result(0)
        if args == ["diff", "--name-only", "--diff-filter=U"]:
            return _make_git_result(0, stdout=unmerged_stdout)
        if args == ["merge", "--abort"]:
            return _make_git_result(0)
        if args == ["commit", "--no-edit"]:
            return _make_git_result(0)
        if args == ["merge-base", "--is-ancestor", source, target]:
            return _make_git_result(0)
        return _make_git_result(0)

    return _run_git


@pytest.mark.asyncio
async def test_merge_worktree_success_returns_worktree_path_and_merge_sha():
    """Successful merge returns worktree_path and final target merge SHA."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect()

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert result["worktree_path"] == "/tmp/wt"
    assert result["project_path"] == "/tmp/repo"
    assert result["merged"] is True
    assert result["merge_sha"] == "abc123def456"
    assert result["target_head_sha"] == "abc123def456"
    assert result["commit_sha"] == "abc123def456"


@pytest.mark.asyncio
async def test_merge_worktree_local_only_target_uses_local_branch():
    """Local target branch is merged directly when no remote target exists."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context(base="develop")

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(target="develop")

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
    assert merge_calls[0][0][0] == ["merge", "feat", "--no-edit"]
    assert merge_calls[0].kwargs.get("cwd") == "/tmp/repo"


@pytest.mark.asyncio
async def test_merge_worktree_uses_existing_target_worktree_when_branch_is_checked_out():
    """Merge in the target worktree when git already has the target branch checked out."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    target_worktree = MagicMock()
    target_worktree.branch = "main"
    target_worktree.path = "/tmp/target-wt"
    ctx.git_manager.list_worktrees.return_value = [target_worktree]
    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        merge_result=_make_git_result(0, stdout="Already up to date.\n")
    )

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert result["merged"] is True
    assert result["merge_sha"] == "abc123def456"
    assert result["target_worktree_path"] == "/tmp/target-wt"

    merge_calls = [
        call
        for call in ctx.git_manager._run_git.call_args_list
        if call[0][0] == ["merge", "feat", "--no-edit"]
    ]
    assert len(merge_calls) == 1
    assert merge_calls[0].kwargs.get("cwd") == "/tmp/target-wt"

    checkout_calls = [
        call for call in ctx.git_manager._run_git.call_args_list if call[0][0][:1] == ["checkout"]
    ]
    assert checkout_calls == []


@pytest.mark.asyncio
async def test_merge_worktree_prefer_remote_is_rejected():
    """Remote target selection is rejected; worktree merges are local-only."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context(base="develop")

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(target="develop")

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123", prefer_remote=True)

    assert result["success"] is False
    assert "local target branch" in result["error"]
    ctx.git_manager._run_git.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_conflict_returns_worktree_path():
    """Merge with conflicts returns worktree_path."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        merge_result=_make_git_result(1, stderr="CONFLICT"),
        unmerged_stdout="src/main.py\n",
    )

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

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        merge_result=_make_git_result(1, stderr="CONFLICT"),
        unmerged_stdout=".gobby/tasks.jsonl\n",
    )

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
async def test_merge_worktree_push_true_is_rejected_without_git_commands():
    """push=True is rejected before any git command can run."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect()

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123", push=True)

    assert result["success"] is False
    assert "never pushes" in result["error"]
    ctx.git_manager._run_git.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_non_conflict_error_returns_worktree_path():
    """Non-conflict merge error returns worktree_path."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        merge_result=_make_git_result(128, stdout="fatal: not a git repo", stderr=""),
        unmerged_stdout="",
    )

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
async def test_merge_worktree_allows_disjoint_target_dirt():
    """Target checkout dirt is allowed when incoming merge does not touch it."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        status_stdout=" D docs/plans/one-surface-ui-draft.md\n",
        incoming_stdout="src/gobby/cli/postgres.py\n",
    )

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is True
    assert result["merged"] is True
    assert result["merge_sha"] == "abc123def456"
    ctx.worktree_storage.mark_merged.assert_called_once_with("wt-123")


@pytest.mark.asyncio
async def test_merge_worktree_rejects_overlapping_target_dirt():
    """Target checkout dirt still blocks when source changes the same path."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()
    ctx.git_manager._run_git.side_effect = _local_merge_side_effect(
        status_stdout=" M src/gobby/cli/postgres.py\n",
        incoming_stdout="src/gobby/cli/postgres.py\n",
    )

    registry = create_sync_registry(ctx)
    merge_tool = registry.get_tool("merge_worktree")

    with patch(
        "gobby.mcp_proxy.tools.worktrees._sync.resolve_project_context",
        return_value=(ctx.git_manager, "test-project", None),
    ):
        result = await merge_tool("wt-123")

    assert result["success"] is False
    assert result["error"] == "Target checkout has uncommitted changes that overlap merge"
    assert result["dirty_files"] == [" M src/gobby/cli/postgres.py"]
    assert result["overlapping_dirty_paths"] == ["src/gobby/cli/postgres.py"]
    assert ["merge", "feat", "--no-edit"] not in [
        call.args[0] for call in ctx.git_manager._run_git.call_args_list
    ]
    ctx.worktree_storage.mark_merged.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_stash_restores_on_success():
    """Stash pop is called after successful merge."""
    from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

    ctx = _make_registry_context()

    ctx.git_manager._run_git.side_effect = _local_merge_side_effect()

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
