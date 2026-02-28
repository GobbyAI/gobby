"""Tests for workflow dry-run evaluator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import WorkflowDefinition, WorkflowStep, WorkflowTransition
from gobby.workflows.dry_run import (
    EvaluationItem,
    WorkflowEvaluation,
    evaluate_workflow,
)

pytestmark = pytest.mark.unit


def _make_step(
    name: str,
    transitions: list[dict[str, str]] | None = None,
    on_enter: list[dict[str, str]] | None = None,
    description: str | None = None,
    allowed_tools: list[str] | str = "all",
    blocked_tools: list[str] | None = None,
    allowed_mcp_tools: list[str] | str = "all",
    blocked_mcp_tools: list[str] | None = None,
    on_mcp_success: list[dict[str, str]] | None = None,
    on_mcp_error: list[dict[str, str]] | None = None,
) -> WorkflowStep:
    """Helper to create a WorkflowStep."""
    return WorkflowStep(
        name=name,
        description=description,
        transitions=[WorkflowTransition(**t) for t in (transitions or [])],
        on_enter=on_enter or [],
        allowed_tools=allowed_tools,
        blocked_tools=blocked_tools or [],
        allowed_mcp_tools=allowed_mcp_tools,
        blocked_mcp_tools=blocked_mcp_tools or [],
        on_mcp_success=on_mcp_success or [],
        on_mcp_error=on_mcp_error or [],
    )


def _make_definition(
    name: str = "test-workflow",
    steps: list[WorkflowStep] | None = None,
    variables: dict[str, str] | None = None,
    wf_type: str = "step",
    exit_condition: str | None = None,
) -> WorkflowDefinition:
    """Helper to create a WorkflowDefinition."""
    return WorkflowDefinition(
        name=name,
        type=wf_type,
        steps=steps or [],
        variables=variables or {},
        exit_condition=exit_condition,
    )


@pytest.fixture
def mock_loader() -> MagicMock:
    loader = MagicMock()
    loader.load_workflow = AsyncMock(return_value=None)
    return loader


class TestWorkflowNotFound:
    @pytest.mark.asyncio
    async def test_workflow_not_found(self, mock_loader: MagicMock) -> None:
        """Returns valid=False and WORKFLOW_NOT_FOUND error."""
        mock_loader.load_workflow.return_value = None

        result = await evaluate_workflow("nonexistent", mock_loader)

        assert result.valid is False
        assert len(result.errors) == 1
        assert result.errors[0].code == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_workflow_load_error(self, mock_loader: MagicMock) -> None:
        """Returns valid=False and WORKFLOW_LOAD_ERROR on ValueError."""
        mock_loader.load_workflow.side_effect = ValueError("Circular inheritance")

        result = await evaluate_workflow("broken", mock_loader)

        assert result.valid is False
        assert result.errors[0].code == "WORKFLOW_LOAD_ERROR"


class TestLifecycleType:
    @pytest.mark.asyncio
    async def test_lifecycle_type_info(self, mock_loader: MagicMock) -> None:
        """Always-on workflows get info-level type notice."""
        definition = WorkflowDefinition(
            name="lifecycle-wf",
            enabled=True,
            steps=[_make_step("init")],
        )
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("lifecycle-wf", mock_loader)

        info_items = [i for i in result.items if i.code == "ALWAYS_ON"]
        assert len(info_items) == 1
        assert info_items[0].level == "info"


class TestPipelineType:
    @pytest.mark.asyncio
    async def test_pipeline_type_skips_step_checks(self, mock_loader: MagicMock) -> None:
        """Pipeline workflows skip step-based checks."""
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep

        pipeline = PipelineDefinition(
            name="test-pipeline",
            steps=[PipelineStep(id="step1", exec="echo hello")],
        )
        mock_loader.load_workflow.return_value = pipeline

        result = await evaluate_workflow("test-pipeline", mock_loader)

        assert result.valid is True
        assert result.workflow_type == "pipeline"
        assert any(i.code == "PIPELINE_TYPE" for i in result.items)


class TestStructuralValidation:
    @pytest.mark.asyncio
    async def test_no_steps(self, mock_loader: MagicMock) -> None:
        """NO_STEPS error for empty step list."""
        definition = _make_definition(steps=[])
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("empty", mock_loader)

        assert result.valid is False
        assert any(i.code == "NO_STEPS" for i in result.errors)

    @pytest.mark.asyncio
    async def test_undefined_transition_target(self, mock_loader: MagicMock) -> None:
        """UNDEFINED_TRANSITION_TARGET error for transition to nonexistent step."""
        steps = [
            _make_step("start", transitions=[{"to": "nonexistent", "when": "true"}]),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("bad-transition", mock_loader)

        assert result.valid is False
        assert any(i.code == "UNDEFINED_TRANSITION_TARGET" for i in result.errors)

    @pytest.mark.asyncio
    async def test_unreachable_step(self, mock_loader: MagicMock) -> None:
        """UNREACHABLE_STEP warning for disconnected step."""
        steps = [
            _make_step("start", transitions=[{"to": "middle", "when": "true"}]),
            _make_step("middle"),
            _make_step("orphan"),  # Not reachable from start
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("unreachable", mock_loader)

        unreachable_items = [i for i in result.warnings if i.code == "UNREACHABLE_STEP"]
        assert len(unreachable_items) == 1
        assert unreachable_items[0].detail["step"] == "orphan"

    @pytest.mark.asyncio
    async def test_dead_end_non_terminal(self, mock_loader: MagicMock) -> None:
        """DEAD_END_STEP warning for non-final step with no transitions."""
        steps = [
            _make_step("start", transitions=[{"to": "dead", "when": "true"}]),
            _make_step("dead"),  # No transitions, not last
            _make_step("end"),  # Last step, dead-end is OK
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("dead-end", mock_loader)

        dead_items = [i for i in result.warnings if i.code == "DEAD_END_STEP"]
        assert len(dead_items) == 1
        assert dead_items[0].detail["step"] == "dead"

    @pytest.mark.asyncio
    async def test_duplicate_step_names(self, mock_loader: MagicMock) -> None:
        """DUPLICATE_STEP_NAME error for repeated step names."""
        steps = [
            _make_step("work"),
            _make_step("work"),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("duplicates", mock_loader)

        assert result.valid is False
        assert any(i.code == "DUPLICATE_STEP_NAME" for i in result.errors)

    @pytest.mark.asyncio
    async def test_undefined_variable_ref(self, mock_loader: MagicMock) -> None:
        """UNDEFINED_VARIABLE_REF warning for Jinja refs to undeclared variables."""
        steps = [
            _make_step(
                "start",
                on_enter=[
                    {"type": "inject_message", "content": "Hello {{ variables.unknown_var }}"},
                ],
            ),
        ]
        definition = _make_definition(steps=steps, variables={"known_var": "value"})
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("undef-var", mock_loader)

        undef_items = [i for i in result.warnings if i.code == "UNDEFINED_VARIABLE_REF"]
        assert len(undef_items) == 1
        assert undef_items[0].detail["variable"] == "unknown_var"

    @pytest.mark.asyncio
    async def test_builtin_variable_not_flagged(self, mock_loader: MagicMock) -> None:
        """Built-in variables like session_id should not trigger warnings."""
        steps = [
            _make_step(
                "start",
                on_enter=[
                    {"type": "inject_message", "content": "Session: {{ variables.session_id }}"},
                ],
            ),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("builtin-var", mock_loader)

        undef_items = [i for i in result.warnings if i.code == "UNDEFINED_VARIABLE_REF"]
        assert len(undef_items) == 0

    @pytest.mark.asyncio
    async def test_mcp_tool_conflict(self, mock_loader: MagicMock) -> None:
        """MCP_TOOL_RESTRICTION_CONFLICT warning for same tool in allowed and blocked."""
        steps = [
            _make_step(
                "start",
                allowed_mcp_tools=["gobby-tasks:create_task", "gobby-tasks:list_tasks"],
                blocked_mcp_tools=["gobby-tasks:create_task"],
            ),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("mcp-conflict", mock_loader)

        conflict_items = [i for i in result.warnings if i.code == "MCP_TOOL_RESTRICTION_CONFLICT"]
        assert len(conflict_items) == 1

    @pytest.mark.asyncio
    async def test_tool_restriction_conflict(self, mock_loader: MagicMock) -> None:
        """TOOL_RESTRICTION_CONFLICT warning for same tool in allowed and blocked."""
        steps = [
            _make_step(
                "start",
                allowed_tools=["Read", "Write", "Edit"],
                blocked_tools=["Write"],
            ),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("tool-conflict", mock_loader)

        conflict_items = [i for i in result.warnings if i.code == "TOOL_RESTRICTION_CONFLICT"]
        assert len(conflict_items) == 1

    @pytest.mark.asyncio
    async def test_circular_only_path(self, mock_loader: MagicMock) -> None:
        """CIRCULAR_ONLY_PATH warning when all paths loop."""
        steps = [
            _make_step("a", transitions=[{"to": "b", "when": "true"}]),
            _make_step("b", transitions=[{"to": "a", "when": "true"}]),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("circular", mock_loader)

        circular_items = [i for i in result.warnings if i.code == "CIRCULAR_ONLY_PATH"]
        assert len(circular_items) == 1


class TestSemanticValidation:
    @pytest.mark.asyncio
    async def test_semantic_checks_skipped(self, mock_loader: MagicMock) -> None:
        """Info when mcp_manager is None."""
        steps = [_make_step("start")]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("test", mock_loader, mcp_manager=None)

        skipped_items = [i for i in result.items if i.code == "SEMANTIC_CHECKS_SKIPPED"]
        assert len(skipped_items) == 1

    @pytest.mark.asyncio
    async def test_unknown_mcp_server(self, mock_loader: MagicMock) -> None:
        """UNKNOWN_MCP_SERVER warning with mcp_manager."""
        steps = [
            _make_step(
                "start",
                allowed_mcp_tools=["fake-server:some_tool"],
            ),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-tasks"]
        mcp_manager.list_tools = AsyncMock(
            return_value={
                "gobby-tasks": [{"name": "create_task"}, {"name": "list_tasks"}],
            }
        )

        result = await evaluate_workflow("test", mock_loader, mcp_manager=mcp_manager)

        unknown_items = [i for i in result.warnings if i.code == "UNKNOWN_MCP_SERVER"]
        assert len(unknown_items) == 1

    @pytest.mark.asyncio
    async def test_unknown_mcp_tool(self, mock_loader: MagicMock) -> None:
        """UNKNOWN_MCP_TOOL warning for tool not found on known server."""
        steps = [
            _make_step(
                "start",
                allowed_mcp_tools=["gobby-tasks:nonexistent_tool"],
            ),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-tasks"]
        mcp_manager.list_tools = AsyncMock(
            return_value={
                "gobby-tasks": [{"name": "create_task"}],
            }
        )

        result = await evaluate_workflow("test", mock_loader, mcp_manager=mcp_manager)

        unknown_items = [i for i in result.warnings if i.code == "UNKNOWN_MCP_TOOL"]
        assert len(unknown_items) == 1

    @pytest.mark.asyncio
    async def test_unknown_mcp_action_target(self, mock_loader: MagicMock) -> None:
        """UNKNOWN_MCP_ACTION_TARGET warning for on_enter call_mcp_tool."""
        steps = [
            _make_step(
                "start",
                on_enter=[
                    {
                        "type": "call_mcp_tool",
                        "server_name": "fake-server",
                        "tool_name": "do_stuff",
                    },
                ],
            ),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-tasks"]
        mcp_manager.list_tools = AsyncMock(
            return_value={
                "gobby-tasks": [{"name": "create_task"}],
            }
        )

        result = await evaluate_workflow("test", mock_loader, mcp_manager=mcp_manager)

        unknown_items = [i for i in result.warnings if i.code == "UNKNOWN_MCP_ACTION_TARGET"]
        assert len(unknown_items) == 1


class TestStepTrace:
    @pytest.mark.asyncio
    async def test_step_trace_complete(self, mock_loader: MagicMock) -> None:
        """All steps are traced correctly."""
        steps = [
            _make_step(
                "claim_task",
                description="Claim a task",
                transitions=[{"to": "work", "when": "true"}],
                on_enter=[
                    {
                        "type": "call_mcp_tool",
                        "server_name": "gobby-tasks",
                        "tool_name": "claim_task",
                    },
                ],
            ),
            _make_step(
                "work",
                description="Do the work",
                transitions=[
                    {"to": "report", "when": "task_tree_complete(variables.session_task)"}
                ],
            ),
            _make_step(
                "report",
                description="Report results",
                transitions=[{"to": "shutdown", "when": "true"}],
            ),
            _make_step(
                "shutdown",
                description="Clean up",
                transitions=[{"to": "complete", "when": "true"}],
            ),
            _make_step("complete", description="Done"),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("traced", mock_loader)

        assert len(result.step_trace) == 5
        assert result.step_trace[0].name == "claim_task"
        assert result.step_trace[0].on_enter_actions == ["call_mcp_tool: gobby-tasks:claim_task"]
        assert result.step_trace[4].name == "complete"


class TestLifecyclePath:
    @pytest.mark.asyncio
    async def test_lifecycle_path_linear(self, mock_loader: MagicMock) -> None:
        """Linear path is traced correctly."""
        steps = [
            _make_step("claim_task", transitions=[{"to": "work", "when": "true"}]),
            _make_step("work", transitions=[{"to": "report", "when": "true"}]),
            _make_step("report", transitions=[{"to": "shutdown", "when": "true"}]),
            _make_step("shutdown", transitions=[{"to": "complete", "when": "true"}]),
            _make_step("complete"),
        ]
        definition = _make_definition(steps=steps)
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("linear", mock_loader)

        assert result.lifecycle_path == ["claim_task", "work", "report", "shutdown", "complete"]


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_valid_workflow_happy_path(self, mock_loader: MagicMock) -> None:
        """No errors, valid=True for well-formed workflow."""
        steps = [
            _make_step("start", transitions=[{"to": "work", "when": "true"}]),
            _make_step("work", transitions=[{"to": "end", "when": "true"}]),
            _make_step("end"),
        ]
        definition = _make_definition(steps=steps, variables={"foo": "bar"})
        mock_loader.load_workflow.return_value = definition

        result = await evaluate_workflow("happy", mock_loader)

        assert result.valid is True
        assert len(result.errors) == 0
        assert result.variables_declared == ["foo"]


class TestToDict:
    def test_evaluation_item_to_dict(self) -> None:
        """EvaluationItem serializes correctly."""
        item = EvaluationItem(
            layer="structure",
            level="error",
            code="NO_STEPS",
            message="No steps",
            detail={"foo": "bar"},
        )
        d = item.to_dict()
        assert d["layer"] == "structure"
        assert d["level"] == "error"
        assert d["code"] == "NO_STEPS"
        assert d["detail"] == {"foo": "bar"}

    def test_workflow_evaluation_to_dict(self) -> None:
        """WorkflowEvaluation serializes correctly."""
        result = WorkflowEvaluation(
            valid=True,
            workflow_name="test",
            workflow_type="step",
        )
        d = result.to_dict()
        assert d["valid"] is True
        assert d["workflow_name"] == "test"
        assert d["items"] == []
