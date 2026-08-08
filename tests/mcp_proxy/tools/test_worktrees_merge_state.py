"""Unit tests for git-backed worktree merge-state helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.worktrees._merge_state import is_branch_ancestor

pytestmark = pytest.mark.unit


def _git_manager(returncode: int) -> MagicMock:
    manager = MagicMock()
    manager.run_git_command.return_value = SimpleNamespace(
        returncode=returncode, stdout="", stderr=""
    )
    return manager


def test_is_branch_ancestor_single_fully_qualified_check() -> None:
    manager = _git_manager(returncode=0)

    assert is_branch_ancestor(manager, "task-1-branch", "0.5.0", cwd="/repo") is True

    manager.run_git_command.assert_called_once_with(
        ["merge-base", "--is-ancestor", "refs/heads/task-1-branch", "refs/heads/0.5.0"],
        cwd="/repo",
        timeout=10,
    )


def test_is_branch_ancestor_no_origin_fallback_on_failure() -> None:
    """A not-merged local branch is reported not-merged: no origin/* retries."""
    manager = _git_manager(returncode=1)

    assert is_branch_ancestor(manager, "task-1-branch", "0.5.0", cwd="/repo") is False

    assert manager.run_git_command.call_count == 1


def test_is_branch_ancestor_explicit_remote_target_is_qualified() -> None:
    manager = _git_manager(returncode=0)

    assert is_branch_ancestor(manager, "task-1-branch", "origin/main", cwd="/repo") is True

    manager.run_git_command.assert_called_once_with(
        ["merge-base", "--is-ancestor", "refs/heads/task-1-branch", "refs/remotes/origin/main"],
        cwd="/repo",
        timeout=10,
    )
