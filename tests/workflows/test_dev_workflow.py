"""Red tests for the interactive /gobby dev workflow contract."""

from __future__ import annotations

import pytest

from tests.workflows.interactive_workflow_helpers import has_spawn_agent_step, load_workflow

pytestmark = pytest.mark.unit

WORKFLOW_PATH = "src/gobby/install/shared/workflows/dev.yaml"


def test_developer_agent_wired() -> None:
    workflow = load_workflow(WORKFLOW_PATH)

    assert workflow["name"] == "dev"
    assert has_spawn_agent_step(workflow, "developer")
    assert "task_id" in workflow.get("inputs", {})
