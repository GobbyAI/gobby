"""Focused tests for the E2E real-home write guard."""

from __future__ import annotations

import pytest

from tests.e2e.conftest import _is_worktree_tool_artifact


@pytest.mark.parametrize(
    "rel_path",
    [
        "worktrees/gobby/review-fixes-16942-llm/.ruff_cache/0.14.13/cache-entry",
        "worktrees/gobby/review-fixes-16937-daemon-core/src/__pycache__/runner.pyc",
    ],
)
def test_worktree_tool_artifacts_are_exempt(rel_path: str) -> None:
    assert _is_worktree_tool_artifact(rel_path) is True


@pytest.mark.parametrize(
    "rel_path",
    [
        "worktrees/gobby/review-fixes-16942-llm/escaped.json",
        "config.json",
        "logs/daemon.log",
    ],
)
def test_non_tool_files_are_not_exempt(rel_path: str) -> None:
    assert _is_worktree_tool_artifact(rel_path) is False
