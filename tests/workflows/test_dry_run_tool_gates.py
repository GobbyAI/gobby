"""Tests for static tool-gate validation in the workflow dry-run evaluator.

A typo in a blocked_tools/blocked_mcp_tools entry fails open at runtime (the
gate never matches, the tool is silently allowed), so unknown references in
blocking gates are errors while unknown references in allow gates are
warnings. The drift canary at the bottom holds every bundled template to the
error-free bar so a typo'd security gate fails CI.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from gobby.mcp_proxy.manager import MCPClientManager, MCPServerConfig
from gobby.mcp_proxy.tools.workflows import workflow_mcp_inventory
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    AgentStepWorkflowBody,
    WorkflowDefinition,
    WorkflowStep,
)
from gobby.workflows.dry_run import (
    WorkflowEvaluation,
    check_agent_tool_gates,
    check_step_tool_gates,
    evaluate_agent_definition,
)
from gobby.workflows.native_tools import is_known_native_tool

pytestmark = pytest.mark.unit

BUNDLED_WORKFLOWS_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "gobby" / "install" / "shared" / "workflows"
)


def _evaluation() -> WorkflowEvaluation:
    return WorkflowEvaluation(valid=True, workflow_name="test")


def _findings(result: WorkflowEvaluation, code: str) -> list[str]:
    return [i.message for i in result.items if i.code == code]


class TestNativeToolCatalog:
    def test_known_native_tools(self) -> None:
        for name in ("Bash", "Workflow", "Task", "LS", "shell", "run_shell_command"):
            assert is_known_native_tool(name), name

    def test_mcp_passthrough_recognized(self) -> None:
        assert is_known_native_tool("mcp__gobby__call_tool")
        assert is_known_native_tool("mcp__brave-search__brave_web_search")
        assert is_known_native_tool("mcp__gobby")

    def test_typo_and_case_mismatch_rejected(self) -> None:
        assert not is_known_native_tool("Wokflow")
        assert not is_known_native_tool("bash")
        assert not is_known_native_tool("mcp_gobby__call_tool")


class TestStepToolGates:
    def test_typo_in_blocked_tools_is_error(self) -> None:
        step = WorkflowStep(name="work", blocked_tools=["Wokflow", "Task"])
        result = _evaluation()
        check_step_tool_gates(step, result)
        errors = [i for i in result.items if i.level == "error"]
        assert len(errors) == 1
        assert errors[0].code == "UNKNOWN_NATIVE_TOOL"
        assert "Wokflow" in errors[0].message
        assert "fail-open" in errors[0].message

    def test_unknown_in_allowed_tools_is_warning(self) -> None:
        step = WorkflowStep(name="work", allowed_tools=["Bash", "Reed"])
        result = _evaluation()
        check_step_tool_gates(step, result)
        assert not [i for i in result.items if i.level == "error"]
        warnings = [i for i in result.items if i.level == "warning"]
        assert len(warnings) == 1
        assert "Reed" in warnings[0].message

    def test_mcp_passthrough_in_allowed_tools_not_flagged(self) -> None:
        step = WorkflowStep(
            name="work",
            allowed_tools=[
                "mcp__gobby__call_tool",
                "mcp__gobby__list_mcp_servers",
                "mcp__gobby__list_tools",
                "mcp__gobby__get_tool_schema",
            ],
        )
        result = _evaluation()
        check_step_tool_gates(step, result)
        assert result.items == []

    def test_mcp_ref_in_native_tool_gate_is_wrong_field_error(self) -> None:
        step = WorkflowStep(name="work", blocked_tools=["gobby-agents:kill_agent"])
        result = _evaluation()
        check_step_tool_gates(step, result)
        errors = [i for i in result.items if i.level == "error"]
        assert len(errors) == 1
        assert errors[0].code == "MCP_TOOL_REF_IN_NATIVE_GATE"
        assert "blocked_mcp_tools" in errors[0].message

    def test_allowed_tools_all_skipped(self) -> None:
        step = WorkflowStep(name="work", allowed_tools="all")
        result = _evaluation()
        check_step_tool_gates(step, result)
        assert result.items == []

    def test_malformed_blocked_mcp_ref_is_error(self) -> None:
        step = WorkflowStep(name="work", blocked_mcp_tools=["kill_agent"])
        result = _evaluation()
        check_step_tool_gates(step, result)
        errors = [i for i in result.items if i.level == "error"]
        assert len(errors) == 1
        assert errors[0].code == "MALFORMED_MCP_TOOL_REF"

    def test_malformed_allowed_mcp_ref_is_warning(self) -> None:
        step = WorkflowStep(name="work", allowed_mcp_tools=["claim_task"])
        result = _evaluation()
        check_step_tool_gates(step, result)
        warnings = [i for i in result.items if i.level == "warning"]
        assert len(warnings) == 1
        assert warnings[0].code == "MALFORMED_MCP_TOOL_REF"

    def test_wellformed_mcp_refs_pass_static_check(self) -> None:
        step = WorkflowStep(
            name="work",
            blocked_mcp_tools=["gobby-agents:kill_agent", "gobby-tasks:*"],
        )
        result = _evaluation()
        check_step_tool_gates(step, result)
        assert result.items == []


class TestAgentToolGates:
    def test_agent_level_typo_in_blocked_tools_is_error(self) -> None:
        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="merge-worker",
            blocked_tools=["Wokflow", "Task"],
        )
        result = _evaluation()
        check_agent_tool_gates(agent, result)
        errors = [i for i in result.items if i.level == "error"]
        assert len(errors) == 1
        assert "Agent 'merge-worker'" in errors[0].message

    def test_agent_inline_steps_are_linted(self) -> None:
        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="worker",
            step_workflow=AgentStepWorkflowBody(
                steps=[WorkflowStep(name="work", blocked_tools=["Wokflow"])],
            ),
        )
        result = _evaluation()
        check_agent_tool_gates(agent, result)
        assert _findings(result, "UNKNOWN_NATIVE_TOOL")

    @pytest.mark.asyncio
    async def test_agent_blocked_mcp_unknown_tool_is_error(self) -> None:
        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="worker",
            blocked_mcp_tools=["gobby-agents:kill_agentt"],
        )
        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-agents"]
        mcp_manager.list_tools = AsyncMock(return_value={"gobby-agents": [{"name": "kill_agent"}]})
        result = await evaluate_agent_definition(agent, mcp_manager)
        assert not result.valid
        errors = [i for i in result.items if i.level == "error"]
        assert [i.code for i in errors] == ["UNKNOWN_MCP_TOOL"]

    @pytest.mark.asyncio
    async def test_clean_agent_is_valid(self) -> None:
        agent = AgentDefinitionBody(
            prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
            name="worker",
            blocked_tools=["Workflow", "Task"],
            blocked_mcp_tools=["gobby-agents:kill_agent"],
        )
        result = await evaluate_agent_definition(agent)
        assert result.valid


class TestBlockedMcpSemanticSeverity:
    @pytest.mark.asyncio
    async def test_unknown_tool_in_step_blocked_mcp_is_error(self) -> None:
        definition = WorkflowDefinition(
            name="wf",
            type="step",
            steps=[
                WorkflowStep(
                    name="work",
                    blocked_mcp_tools=["gobby-agents:kill_agentt"],
                )
            ],
        )
        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-agents"]
        mcp_manager.list_tools = AsyncMock(return_value={"gobby-agents": [{"name": "kill_agent"}]})
        result = await evaluate_agent_definition(
            AgentDefinitionBody(
                prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                name="wf",
                provider="claude",
                step_workflow=AgentStepWorkflowBody(steps=definition.steps),
            ),
            mcp_manager=mcp_manager,
        )
        errors = [i for i in result.items if i.code == "UNKNOWN_MCP_TOOL"]
        assert errors and all(i.level == "error" for i in errors)
        assert not result.valid

    @pytest.mark.asyncio
    async def test_unknown_tool_in_step_allowed_mcp_stays_warning(self) -> None:
        definition = WorkflowDefinition(
            name="wf",
            type="step",
            steps=[WorkflowStep(name="work", allowed_mcp_tools=["gobby-agents:kill_agentt"])],
        )
        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-agents"]
        mcp_manager.list_tools = AsyncMock(return_value={"gobby-agents": [{"name": "kill_agent"}]})
        result = await evaluate_agent_definition(
            AgentDefinitionBody(
                prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                name="wf",
                provider="claude",
                step_workflow=AgentStepWorkflowBody(steps=definition.steps),
            ),
            mcp_manager=mcp_manager,
        )
        warnings = [i for i in result.items if i.code == "UNKNOWN_MCP_TOOL"]
        assert warnings and all(i.level == "warning" for i in warnings)
        assert result.valid


def _iter_bundled(pattern: str) -> list[Path]:
    paths = sorted(BUNDLED_WORKFLOWS_ROOT.glob(pattern))
    assert paths, f"no bundled templates matched {pattern}"
    return paths


class TestBundledTemplateDriftCanary:
    """Every bundled gate entry must be recognized — a typo'd block gate fails CI."""

    @pytest.mark.parametrize("path", _iter_bundled("*.yaml"), ids=lambda p: p.name)
    def test_bundled_workflow_gates_clean(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict) or data.get("type") == "pipeline":
            return
        data.setdefault("name", path.stem)
        definition = WorkflowDefinition.model_validate(data)
        result = _evaluation()
        for step in definition.steps:
            check_step_tool_gates(step, result)
        errors = [i for i in result.items if i.level == "error"]
        assert not errors, [i.message for i in errors]

    @pytest.mark.parametrize("path", _iter_bundled("agents/*.yaml"), ids=lambda p: p.name)
    def test_bundled_agent_gates_clean(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text())
        assert isinstance(data, dict)
        data.setdefault("name", path.stem)
        agent = AgentDefinitionBody.model_validate(data)
        result = _evaluation()
        check_agent_tool_gates(agent, result)
        errors = [i for i in result.items if i.level == "error"]
        assert not errors, [i.message for i in errors]


@pytest.mark.asyncio
async def test_workflow_inventory_is_scoped_to_workflow_project() -> None:
    from uuid import uuid4

    project_a = str(uuid4())
    project_b = str(uuid4())
    config_a = MCPServerConfig(
        name="github",
        project_id=project_a,
        transport="http",
        url="http://a.example",
    )
    config_b = MCPServerConfig(
        name="github",
        project_id=project_b,
        transport="http",
        url="http://b.example",
    )
    manager = MCPClientManager(server_configs=[config_a, config_b])
    manager._tool_schema_cache[config_a.id] = [{"name": "create_issue_a"}]
    manager._tool_schema_cache[config_b.id] = [{"name": "create_issue_b"}]

    inventory_a = workflow_mcp_inventory(
        None,
        lambda: manager,
        lambda: project_a,
    )
    inventory_b = workflow_mcp_inventory(
        None,
        lambda: manager,
        lambda: project_b,
    )
    assert inventory_a is not None
    assert inventory_b is not None
    assert inventory_a.get_available_servers() == ["github"]
    assert inventory_b.get_available_servers() == ["github"]
    tools_a = await inventory_a.list_tools()
    tools_b = await inventory_b.list_tools()
    assert tools_a == {"github": [{"name": "create_issue_a"}]}
    assert tools_b == {"github": [{"name": "create_issue_b"}]}
