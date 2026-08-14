"""Tests for clean get_workflow not-found responses."""

import json

import pytest

from gobby.mcp_proxy.tools.workflows import create_workflows_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.pipeline_loader import PipelineLoader


@pytest.mark.asyncio
async def test_get_workflow_returns_not_found_for_rule_row(
    temp_db: HubDatabase,
) -> None:
    rule_name = "require-claimed-task-required-skills"
    definitions = LocalWorkflowDefinitionManager(temp_db)
    definitions.create(
        name=rule_name,
        definition_json=json.dumps(
            {
                "when": "true",
                "event": "before_tool",
                "effects": [{"type": "block", "reason": "test"}],
            }
        ),
        workflow_type="rule",
    )
    registry = create_workflows_registry(db=temp_db, loader=PipelineLoader(db=temp_db))

    result = await registry.call("get_workflow", {"name": rule_name})

    assert result == {
        "success": False,
        "error": f"Workflow '{rule_name}' not found",
    }
