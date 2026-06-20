"""Tests for spawn_agent runtime response helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.mcp_proxy.tools.spawn_agent._runtime import _build_spawn_success_response

pytestmark = pytest.mark.unit


def test_build_spawn_success_response_serializes_paths_and_omits_null_reasoning() -> None:
    response = _build_spawn_success_response(
        run_id="run-123",
        spawn_result=SimpleNamespace(
            child_session_id="child-123",
            status="running",
            pid=123,
            message="spawned",
        ),
        effective_isolation="worktree",
        isolation_ctx=SimpleNamespace(
            branch_name="branch",
            worktree_id="wt-123",
            clone_id=None,
            cwd=Path("/tmp/worktree"),
        ),
        base_commit_sha="abc123",
        tmux_session_name=None,
        tmux_socket_name=None,
        tmux_socket_path=None,
        code_index_preflight_warning=None,
        reasoning=None,
    )

    assert response["worktree_path"] == "/tmp/worktree"
    assert response["clone_path"] is None
    assert "reasoning" not in response
