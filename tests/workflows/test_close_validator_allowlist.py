"""Contract tests for the bundled task-close-validator step allowlist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

AGENT_PATH = (
    Path(__file__).resolve().parents[2]
    / "src/gobby/install/shared/workflows/agents/task-close-validator.yaml"
)

READ_ONLY_TOOLS = {
    "gobby-memory:search_memories",
    "gobby-memory:get_memory",
    "gobby-workflows:get_rule",
}

EXPECTED_ALLOWED_TOOLS = {
    "gobby-tasks:get_task",
    "gobby-tasks:list_tasks",
    "gobby-tasks:submit_close_review",
    "gobby-agents:end_agent_run",
    *READ_ONLY_TOOLS,
}

LIFECYCLE_MUTATION_TOOLS = {
    "gobby-tasks:create_task",
    "gobby-tasks:claim_task",
    "gobby-tasks:update_task",
    "gobby-tasks:delete_task",
    "gobby-tasks:close_task",
    "gobby-tasks:reopen_task",
    "gobby-tasks:escalate_task",
    "gobby-tasks:link_commit",
    "gobby-tasks:unlink_commit",
    "gobby-tasks-ops:submit_for_review",
    "gobby-tasks-ops:approve_review",
    "gobby-tasks-ops:reject_review",
    "gobby-tasks-ops:complete_stage",
    "gobby-tasks-ops:fail_stage",
    "gobby-agents:spawn_agent",
    "gobby-agents:stop_agent",
    "gobby-agents:kill_agent",
}


def _review_step() -> dict[str, Any]:
    data = yaml.safe_load(AGENT_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    steps = data["step_workflow"]["steps"]
    return next(step for step in steps if step["name"] == "review")


def test_review_step_allows_only_required_readers_and_terminal_tools() -> None:
    """Regression for #22566: close validators need audited read access."""
    review = _review_step()
    allowed = set(review["allowed_mcp_tools"])
    blocked = set(review["blocked_mcp_tools"])

    assert allowed == EXPECTED_ALLOWED_TOOLS
    assert READ_ONLY_TOOLS.isdisjoint(blocked)


def test_review_step_keeps_lifecycle_mutations_blocked() -> None:
    """The validator cannot mutate task lifecycle state or manage agents."""
    review = _review_step()
    allowed = set(review["allowed_mcp_tools"])
    blocked = set(review["blocked_mcp_tools"])

    assert LIFECYCLE_MUTATION_TOOLS <= blocked
    assert LIFECYCLE_MUTATION_TOOLS.isdisjoint(allowed)
