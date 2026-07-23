"""Red tests for the interactive /gobby review workflow contract."""

from __future__ import annotations

import pytest

from tests.workflows.interactive_workflow_helpers import (
    has_spawn_agent_step,
    load_workflow,
    workflow_text,
)

pytestmark = pytest.mark.unit

WORKFLOW_PATH = "src/gobby/install/shared/workflows/review.yaml"


def test_epic_reviewer_wired() -> None:
    workflow = load_workflow(WORKFLOW_PATH)
    body = workflow_text(WORKFLOW_PATH)

    assert workflow["name"] == "review"
    assert has_spawn_agent_step(workflow, "epic-reviewer")
    assert "task_id" in workflow.get("inputs", {})
    assert "approve" in body.lower()
    assert "reject" in body.lower()
    assert "escalate" in body.lower()


# Keep in sync with the spawn_agent signature in
# src/gobby/mcp_proxy/tools/spawn_agent/_factory.py.
SPAWN_AGENT_PARAMETERS = frozenset(
    {
        "prompt",
        "agent",
        "task_id",
        "allow_closed_task",
        "isolation",
        "branch_name",
        "base_branch",
        "clone_id",
        "worktree_id",
        "workflow",
        "provider",
        "model",
        "reasoning_effort",
        "reasoning_required",
        "timeout",
        "parent_session_id",
        "project_path",
        "notify_parent_on_completion",
    }
)


def test_spawn_step_passes_only_valid_spawn_agent_parameters() -> None:
    """Regression for #18717: assigned_task_id is not a spawn_agent parameter."""
    workflow = load_workflow(WORKFLOW_PATH)
    steps = workflow.get("steps", [])
    spawn_steps = [
        step["mcp"]
        for step in steps
        if isinstance(step.get("mcp"), dict) and step["mcp"].get("tool") == "spawn_agent"
    ]
    assert spawn_steps, "review workflow must contain a spawn_agent step"

    for mcp in spawn_steps:
        unknown = set(mcp.get("arguments", {})) - SPAWN_AGENT_PARAMETERS
        assert not unknown, f"unknown spawn_agent parameters: {sorted(unknown)}"
