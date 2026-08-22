"""Tests for evaluate_spawn MCP tool dependency wiring."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.mcp_proxy.tools.workflows import workflow_mcp_inventory
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody, AgentWorkflows

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_evaluate_spawn_tool_runs_workflow_validation_with_combined_inventory(
    temp_db: HubDatabase,
) -> None:
    agent = AgentDefinitionBody(
        prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
        name="test-agent",
        provider="claude",
        workflows=AgentWorkflows(pipeline="test-workflow"),
    )
    AgentDefinitionManager(temp_db).create(
        name=agent.name,
        definition_json=agent.model_dump(),
        source="installed",
    )

    workflow_loader = MagicMock()
    workflow_loader.validate_pipeline_for_agent = AsyncMock(
        return_value=(False, "workflow rejected")
    )
    workflow_loader.load_pipeline = AsyncMock(return_value=None)

    internal_manager = InternalRegistryManager()
    internal_manager.add_registry(InternalToolRegistry(name="gobby-tasks"))
    external_manager = MagicMock()
    external_manager.get_available_servers.return_value = ["github"]
    external_manager.list_tools = AsyncMock(return_value={"github": []})
    inventory = workflow_mcp_inventory(internal_manager, lambda: external_manager)
    assert inventory is not None

    runner = MagicMock()
    with (
        patch("gobby.utils.project_context.get_project_context", return_value=None),
        patch("gobby.utils.session_context.get_current_session_id", return_value=None),
    ):
        registry = create_agents_registry(
            runner,
            db=temp_db,
            workflow_loader=workflow_loader,
            mcp_inventory=inventory,
        )

    result = await registry._tools["evaluate_spawn"].func(agent="test-agent")

    workflow_loader.validate_pipeline_for_agent.assert_awaited_once()
    assert workflow_loader.validate_pipeline_for_agent.await_args.args[0] == "test-workflow"
    assert result["can_spawn"] is False
    assert {item["code"] for item in result["items"]} >= {
        "WORKFLOW_RESOLVED",
        "WORKFLOW_INVALID_FOR_AGENT",
    }
    assert inventory.get_available_servers() == ["github", "gobby-tasks"]
    assert set(await inventory.list_tools()) == {"github", "gobby-tasks"}
