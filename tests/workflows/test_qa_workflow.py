"""Red tests for the interactive /gobby qa workflow contract."""

from __future__ import annotations

import pytest

from tests.workflows.interactive_workflow_helpers import (
    has_spawn_agent_step,
    load_workflow,
    workflow_text,
)

pytestmark = pytest.mark.unit

WORKFLOW_PATH = "src/gobby/install/shared/workflows/qa.yaml"


def test_qa_reviewer_wired() -> None:
    workflow = load_workflow(WORKFLOW_PATH)
    body = workflow_text(WORKFLOW_PATH)

    assert workflow["name"] == "qa"
    assert has_spawn_agent_step(workflow, "qa-reviewer")
    assert "task_id" in workflow.get("inputs", {})
    assert "approve" in body.lower()
    assert "reject" in body.lower()
    assert "escalate" in body.lower()
