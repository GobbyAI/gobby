"""Tests for spawn_agent runtime response helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.mcp_proxy.tools.spawn_agent._response import build_spawn_response

pytestmark = pytest.mark.unit


def test_build_spawn_response_serializes_paths_and_omits_null_reasoning() -> None:
    response = build_spawn_response(
        run_id="run-123",
        spawn_result=SimpleNamespace(
            child_session_id="child-123",
            status="running",
            pid=123,
            message="spawned",
        ),
        effective_checkout_mode="worktree",
        isolation_ctx=SimpleNamespace(
            branch_name="branch",
            worktree_id="wt-123",
            clone_id=None,
            cwd=Path("/tmp/worktree"),
            extra={},
        ),
        base_commit_sha="abc123",
        terminal=SimpleNamespace(id="terminal-123", backend="native"),
        code_index_preflight_warning=None,
        reasoning=None,
    )

    assert response["worktree_path"] == "/tmp/worktree"
    assert response["clone_path"] is None
    assert response["reuse_outcome"] == "fresh"
    assert "reasoning" not in response


def test_build_spawn_response_reports_reused_worktree() -> None:
    response = build_spawn_response(
        run_id="run-123",
        spawn_result=SimpleNamespace(
            child_session_id="child-123",
            status="running",
            pid=123,
            message="spawned",
        ),
        effective_checkout_mode="worktree",
        isolation_ctx=SimpleNamespace(
            branch_name="branch",
            worktree_id="wt-123",
            clone_id=None,
            cwd=Path("/tmp/worktree"),
            extra={"reused_worktree": True},
        ),
        base_commit_sha="abc123",
        terminal=SimpleNamespace(id="terminal-123", backend="tmux"),
        code_index_preflight_warning=None,
        reasoning=None,
    )

    assert response["reuse_outcome"] == "reused"
    assert response["reused_worktree"] is True
