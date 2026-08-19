"""Registry disposition for the gobby-workflows MCP surface after 5.2."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.workflows import create_workflows_registry
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.pipeline_loader import PipelineLoader

DELETED_TOOLS = frozenset(
    {
        "create_workflow",
        "update_workflow",
        "delete_workflow",
        "export_workflow",
        "restore_workflow",
        "get_workflow",
        "list_workflows",
        "import_workflow",
        "evaluate_workflow",
        "get_workflow_status",
    }
)

REQUIRED_DOMAIN_TOOLS = frozenset(
    {
        "list_rules",
        "get_rule",
        "create_rule",
        "update_rule",
        "delete_rule",
        "toggle_rule",
        "list_variables",
        "get_variable_definition",
        "create_variable",
        "update_variable",
        "delete_variable",
        "export_variable",
        "list_agent_definitions",
        "get_agent_definition",
        "create_agent_definition",
        "toggle_agent_definition",
        "delete_agent_definition",
        "update_agent_rules",
        "update_agent_variables",
        "update_agent_step_workflow",
        "list_pipelines",
        "get_pipeline",
        "create_pipeline",
        "update_pipeline",
        "get_step_status",
        "evaluate_pipeline",
        "evaluate_agent",
        "reload_cache",
    }
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CALLER_MODULES = (
    _REPO_ROOT / "src/gobby/mcp_proxy/tools/workflows/_agents.py",
    _REPO_ROOT / "src/gobby/mcp_proxy/tools/workflows/_rules.py",
    _REPO_ROOT / "src/gobby/mcp_proxy/tools/workflows/_variables.py",
    _REPO_ROOT / "src/gobby/mcp_proxy/tools/workflows/_pipelines.py",
)


def _tool_names() -> set[str]:
    registry = create_workflows_registry(loader=MagicMock())
    return {str(tool["name"]) for tool in registry.list_tools()}


@pytest.mark.unit
def test_registry_inventory_matches_disposition() -> None:
    names = _tool_names()
    leftover = DELETED_TOOLS & names
    assert leftover == set(), f"deleted tools still registered: {sorted(leftover)}"
    missing = REQUIRED_DOMAIN_TOOLS - names
    assert missing == set(), f"domain tools missing: {sorted(missing)}"
    assert "gobby-workflows" == create_workflows_registry(loader=MagicMock()).name


@pytest.mark.unit
def test_get_step_status_is_registered_under_new_name() -> None:
    registry = create_workflows_registry(loader=MagicMock())
    names = {str(tool["name"]) for tool in registry.list_tools()}
    assert "get_step_status" in names
    assert "get_workflow_status" not in names


@pytest.mark.integration
@pytest.mark.asyncio
async def test_evaluate_tools_cover_pipeline_and_agent(temp_db: HubDatabase) -> None:
    PipelineDefinitionManager(temp_db).create(
        name="eval-pipe",
        definition_json={
            "name": "eval-pipe",
            "type": "pipeline",
            "version": "1.0",
            "steps": [{"id": "work", "exec": "echo work"}],
        },
    )
    AgentDefinitionManager(temp_db).create(
        name="eval-agent",
        definition_json=AgentDefinitionBody(name="eval-agent").model_dump(mode="json"),
    )
    registry = create_workflows_registry(db=temp_db, loader=PipelineLoader(db=temp_db))

    pipeline_result = await registry.call("evaluate_pipeline", {"name": "eval-pipe"})
    agent_result = await registry.call("evaluate_agent", {"name": "eval-agent"})
    missing_agent = await registry.call("evaluate_agent", {"name": "no-such-agent"})

    assert pipeline_result["valid"] is True
    assert any(item["code"] == "PIPELINE_TYPE" for item in pipeline_result["items"])
    assert agent_result["valid"] is True
    assert agent_result["workflow_name"] == "eval-agent"
    assert missing_agent.get("valid") is False
    assert "not found" in str(missing_agent.get("error", "")).lower() or any(
        item.get("code") in {"AGENT_NOT_FOUND", "WORKFLOW_NOT_FOUND"}
        for item in missing_agent.get("items", [])
    )


@pytest.mark.unit
def test_sync_registry_is_canonical_fan_out() -> None:
    from gobby.sync_registry import SYNC_TARGETS, sync_bundled_content_to_db

    names = {target[0] for target in SYNC_TARGETS}
    assert {"rules", "agents", "pipelines", "variables", "detection_manifests"} <= names
    signature = inspect.signature(sync_bundled_content_to_db)
    assert "only" in signature.parameters
    assert "skip_types" in signature.parameters


@pytest.mark.unit
def test_auto_export_requires_explicit_kind() -> None:
    from gobby.mcp_proxy.tools.workflows._auto_export import (
        auto_delete_definition,
        auto_export_definition,
        has_gobby_name_collision,
    )

    export_params = inspect.signature(auto_export_definition).parameters
    delete_params = inspect.signature(auto_delete_definition).parameters
    collision_params = inspect.signature(has_gobby_name_collision).parameters
    assert "kind" in export_params
    assert export_params["kind"].default is inspect.Parameter.empty
    assert "kind" in delete_params
    assert "kind" in collision_params
    assert collision_params["kind"].default is inspect.Parameter.empty


@pytest.mark.unit
def test_auto_export_callers_pass_kind_explicitly() -> None:
    for path in _CALLER_MODULES:
        source = path.read_text(encoding="utf-8")
        assert "auto_export_definition(" in source
        assert 'kind="' in source or "kind='" in source, f"{path} must pass kind="


@pytest.mark.unit
def test_definitions_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        __import__("gobby.mcp_proxy.tools.workflows._definitions")


@pytest.mark.unit
def test_generic_crud_suite_is_gone() -> None:
    repo = _REPO_ROOT / "tests/mcp_proxy/tools"
    assert not (repo / "test_workflow_crud.py").exists()
    assert not (repo / "workflows" / "test_get_workflow_not_found.py").exists()
