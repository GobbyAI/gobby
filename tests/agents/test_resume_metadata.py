"""Tests for agent resume metadata serialization helpers."""

from __future__ import annotations

import pytest

from gobby.agents.resume_metadata import build_resume_metadata, json_safe
from gobby.agents.sandbox import SandboxConfig

pytestmark = pytest.mark.unit


def test_json_safe_sorts_set_values() -> None:
    assert json_safe({"beta", "alpha"}) == ["alpha", "beta"]
    assert json_safe({("beta", 2), ("alpha", 1)}) == [["alpha", 1], ["beta", 2]]


@pytest.mark.parametrize("prewarm", [True, False])
def test_snapshot_persists_pre_commit_prewarm_for_resume(prewarm: bool) -> None:
    snapshot = build_resume_metadata(
        provider="codex",
        model=None,
        requested_reasoning_effort=None,
        effective_reasoning_effort=None,
        reasoning_required=False,
        reasoning_status="not_requested",
        reasoning_message=None,
        sandbox_config=SandboxConfig(),
        cwd="/work",
        project_id="proj-1",
        project_path="/work",
        parent_session_id="parent-1",
        checkout_mode="none",
        worktree_id=None,
        clone_id=None,
        branch_name=None,
        base_branch="main",
        base_commit_sha=None,
        task_id=None,
        task_ref=None,
        stage_name=None,
        stage_state=None,
        agent_slug="qa-reviewer",
        workflow=None,
        initial_variables={},
        prewarm_pre_commit_store=prewarm,
    )

    assert snapshot["prewarm_pre_commit_store"] is prewarm
