"""Tests for workflow dry-run evaluator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import WorkflowDefinition, WorkflowStep, WorkflowTransition
from gobby.workflows.agent_models import AgentDefinitionBody, AgentStepWorkflowBody
from gobby.workflows.dry_run import (
    EvaluationItem,
    WorkflowEvaluation,
    evaluate_agent_definition,
    evaluate_pipeline_definition,
)

pytestmark = pytest.mark.unit


def _make_step(
    name: str,
    transitions: list[dict[str, str]] | None = None,
    on_enter: list[dict[str, str]] | None = None,
    on_exit: list[dict[str, str]] | None = None,
    description: str | None = None,
    allowed_tools: list[str] | str = "all",
    blocked_tools: list[str] | None = None,
    allowed_mcp_tools: list[str] | str = "all",
    blocked_mcp_tools: list[str] | None = None,
    on_mcp_success: list[dict[str, str]] | None = None,
    on_mcp_error: list[dict[str, str]] | None = None,
    on_mcp_before: list[dict[str, str]] | None = None,
) -> WorkflowStep:
    """Helper to create a WorkflowStep."""
    return WorkflowStep(
        name=name,
        description=description,
        transitions=[WorkflowTransition(**t) for t in (transitions or [])],
        on_enter=on_enter or [],
        on_exit=on_exit or [],
        allowed_tools=allowed_tools,
        blocked_tools=blocked_tools or [],
        allowed_mcp_tools=allowed_mcp_tools,
        blocked_mcp_tools=blocked_mcp_tools or [],
        on_mcp_success=on_mcp_success or [],
        on_mcp_error=on_mcp_error or [],
        on_mcp_before=on_mcp_before or [],
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


def _as_agent(definition: WorkflowDefinition) -> AgentDefinitionBody:
    return AgentDefinitionBody(
        name=definition.name,
        provider="claude",
        step_workflow=AgentStepWorkflowBody(
            steps=definition.steps,
            variables=definition.variables or {},
            exit_condition=definition.exit_condition,
        ),
    )


@pytest.fixture
def mock_loader() -> MagicMock:
    loader = MagicMock()
    loader.load_pipeline = AsyncMock(return_value=None)
    return loader


class TestWorkflowNotFound:
    @pytest.mark.asyncio
    async def test_workflow_not_found(self, mock_loader: MagicMock) -> None:
        """Returns valid=False and WORKFLOW_NOT_FOUND error."""
        result = await evaluate_pipeline_definition("nonexistent", mock_loader)

        assert result.valid is False
        assert len(result.errors) == 1
        assert result.errors[0].code == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_workflow_load_error(self, mock_loader: MagicMock) -> None:
        """Returns valid=False and WORKFLOW_LOAD_ERROR on ValueError."""
        mock_loader.load_pipeline.side_effect = ValueError("Circular inheritance")

        result = await evaluate_pipeline_definition("broken", mock_loader)

        assert result.valid is False
        assert result.errors[0].code == "WORKFLOW_LOAD_ERROR"


class TestLifecycleType:
    @pytest.mark.asyncio
    async def test_step_type_uses_definition_contract(self) -> None:
        definition = _make_definition(steps=[_make_step("init")], wf_type="step")
        result = await evaluate_agent_definition(_as_agent(definition))
        assert result.valid is True


class TestPipelineType:
    @pytest.mark.asyncio
    async def test_pipeline_type_skips_step_checks(self, mock_loader: MagicMock) -> None:
        """Pipeline workflows skip step-based checks."""
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep

        pipeline = PipelineDefinition(
            name="test-pipeline",
            steps=[PipelineStep(id="step1", exec="echo hello")],
        )
        mock_loader.load_pipeline.return_value = pipeline
        result = await evaluate_pipeline_definition("test-pipeline", mock_loader)

        assert result.valid is True
        assert any(i.code == "PIPELINE_TYPE" for i in result.items)


class TestStructuralValidation:
    @pytest.mark.asyncio
    async def test_no_steps(self) -> None:
        """Empty agent step_workflow is rejected by the nested body."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _as_agent(_make_definition(steps=[]))

    @pytest.mark.asyncio
    async def test_undefined_transition_target(self, mock_loader: MagicMock) -> None:
        """UNDEFINED_TRANSITION_TARGET error for transition to nonexistent step."""
        steps = [
            _make_step("start", transitions=[{"to": "nonexistent", "when": "true"}]),
        ]
        definition = _make_definition(steps=steps)
        result = await evaluate_agent_definition(_as_agent(definition))

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
        result = await evaluate_agent_definition(_as_agent(definition))

        unreachable_items = [i for i in result.warnings if i.code == "UNREACHABLE_STEP"]
        assert len(unreachable_items) == 1
        assert unreachable_items[0].detail["step"] == "orphan"

    @pytest.mark.asyncio
    async def test_dead_end_uses_runtime_exit_condition(self, mock_loader: MagicMock) -> None:
        """Only a step selected by the runtime exit condition is terminal."""
        steps = [
            _make_step(
                "start",
                transitions=[
                    {"to": "dead", "when": "vars.failed"},
                    {"to": "complete", "when": "vars.done"},
                ],
            ),
            _make_step("dead"),
            _make_step("complete"),
        ]
        definition = _make_definition(
            steps=steps,
            exit_condition="current_step == 'complete'",
        )
        result = await evaluate_agent_definition(_as_agent(definition))

        dead_items = [i for i in result.warnings if i.code == "DEAD_END_STEP"]
        assert len(dead_items) == 1
        assert dead_items[0].detail["step"] == "dead"

    @pytest.mark.asyncio
    async def test_last_step_is_not_implicitly_terminal(self, mock_loader: MagicMock) -> None:
        """Step order does not grant runtime exit semantics."""
        definition = _make_definition(
            steps=[
                _make_step("start", transitions=[{"to": "last", "when": "true"}]),
                _make_step("last"),
            ]
        )
        result = await evaluate_agent_definition(_as_agent(definition))

        dead_items = [i for i in result.warnings if i.code == "DEAD_END_STEP"]
        assert [item.detail["step"] for item in dead_items] == ["last"]

    @pytest.mark.asyncio
    async def test_duplicate_step_names(self, mock_loader: MagicMock) -> None:
        """DUPLICATE_STEP_NAME error for repeated step names."""
        steps = [
            _make_step("work"),
            _make_step("work"),
        ]
        definition = _make_definition(steps=steps)
        result = await evaluate_agent_definition(_as_agent(definition))

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
        result = await evaluate_agent_definition(_as_agent(definition))

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
        result = await evaluate_agent_definition(_as_agent(definition))

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
        result = await evaluate_agent_definition(_as_agent(definition))

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
        result = await evaluate_agent_definition(_as_agent(definition))

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
        result = await evaluate_agent_definition(_as_agent(definition))

        circular_items = [i for i in result.warnings if i.code == "CIRCULAR_ONLY_PATH"]
        assert len(circular_items) == 1


class TestConditionValidation:
    @pytest.mark.asyncio
    async def test_transition_rejects_name_outside_runtime_context(
        self, mock_loader: MagicMock
    ) -> None:
        steps = [
            _make_step("start", transitions=[{"to": "done", "when": "variables.ready"}]),
            _make_step("done"),
        ]
        result = await evaluate_agent_definition(_as_agent(_make_definition(steps=steps)))

        finding = next(item for item in result.items if item.code == "CONDITION_UNKNOWN_NAME")
        assert finding.detail == {
            "condition": "variables.ready",
            "condition_type": "transition",
            "names": ["variables"],
            "step": "start",
        }

    @pytest.mark.asyncio
    async def test_broken_exit_condition_is_error(self, mock_loader: MagicMock) -> None:
        result = await evaluate_agent_definition(
            _as_agent(_make_definition(steps=[_make_step("done")], exit_condition="current_step =="))
        )

        assert not result.valid
        assert [item.code for item in result.errors] == ["INVALID_CONDITION_SYNTAX"]

    @pytest.mark.asyncio
    async def test_runtime_condition_contexts_are_accepted(self, mock_loader: MagicMock) -> None:
        steps = [
            _make_step("start", transitions=[{"to": "done", "when": "vars.ready"}]),
            _make_step("done"),
        ]
        result = await evaluate_agent_definition(
            _as_agent(
                _make_definition(
                    steps=steps,
                    exit_condition="current_step == 'done' and variables.finished and vars.ready",
                )
            )
        )

        assert not [item for item in result.items if item.code.startswith("CONDITION_")]


class TestSemanticValidation:
    @pytest.mark.asyncio
    async def test_semantic_checks_skipped(self, mock_loader: MagicMock) -> None:
        """Info when mcp_manager is None."""
        steps = [_make_step("start")]
        definition = _make_definition(steps=steps)
        result = await evaluate_agent_definition(_as_agent(definition), None)

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
        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-tasks"]
        mcp_manager.list_tools = AsyncMock(
            return_value={
                "gobby-tasks": [{"name": "create_task"}, {"name": "list_tasks"}],
            }
        )

        result = await evaluate_agent_definition(_as_agent(definition), mcp_manager=mcp_manager)

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
        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-tasks"]
        mcp_manager.list_tools = AsyncMock(
            return_value={
                "gobby-tasks": [{"name": "create_task"}],
            }
        )

        result = await evaluate_agent_definition(_as_agent(definition), mcp_manager=mcp_manager)

        unknown_items = [i for i in result.warnings if i.code == "UNKNOWN_MCP_TOOL"]
        assert len(unknown_items) == 1

    @pytest.mark.asyncio
    async def test_on_enter_mcp_action_is_reported_as_not_executed(
        self, mock_loader: MagicMock
    ) -> None:
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
        result = await evaluate_agent_definition(_as_agent(definition))

        findings = [i for i in result.warnings if i.code == "ACTION_NOT_EXECUTED"]
        assert [item.detail["field"] for item in findings] == ["on_enter"]

    @pytest.mark.asyncio
    async def test_unknown_mcp_handler_target(self, mock_loader: MagicMock) -> None:
        """UNKNOWN_MCP_HANDLER_TARGET warning for on_mcp_success handlers."""
        steps = [
            _make_step(
                "start",
                on_mcp_success=[
                    {
                        "server": "gobby-merge",
                        "tool": "missing_tool",
                        "action": "set_variable",
                    },
                ],
            ),
        ]
        definition = _make_definition(steps=steps)
        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-merge"]
        mcp_manager.list_tools = AsyncMock(
            return_value={
                "gobby-merge": [{"name": "verify_in_worktree"}],
            }
        )

        result = await evaluate_agent_definition(_as_agent(definition), mcp_manager=mcp_manager)

        unknown_items = [i for i in result.warnings if i.code == "UNKNOWN_MCP_HANDLER_TARGET"]
        assert len(unknown_items) == 1

    @pytest.mark.asyncio
    async def test_on_mcp_before_handler_target_is_checked(self, mock_loader: MagicMock) -> None:
        steps = [
            _make_step(
                "start",
                on_mcp_before=[
                    {"server": "gobby-merge", "tool": "missing_tool", "action": "block"}
                ],
            )
        ]
        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-merge"]
        mcp_manager.list_tools = AsyncMock(
            return_value={"gobby-merge": [{"name": "verify_in_worktree"}]}
        )

        result = await evaluate_agent_definition(
            _as_agent(_make_definition(steps=steps)), mcp_manager=mcp_manager
        )

        assert len([i for i in result.warnings if i.code == "UNKNOWN_MCP_HANDLER_TARGET"]) == 1

    @pytest.mark.asyncio
    async def test_handler_star_is_checked_as_exact_tool_name(self, mock_loader: MagicMock) -> None:
        steps = [
            _make_step(
                "start",
                on_mcp_success=[{"server": "gobby-merge", "tool": "*", "action": "set_variable"}],
            )
        ]
        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-merge"]
        mcp_manager.list_tools = AsyncMock(return_value={"gobby-merge": [{"name": "merge"}]})

        result = await evaluate_agent_definition(
            _as_agent(_make_definition(steps=steps)), mcp_manager=mcp_manager
        )

        assert len([i for i in result.warnings if i.code == "UNKNOWN_MCP_HANDLER_TARGET"]) == 1

    @pytest.mark.asyncio
    async def test_disconnected_server_skips_tool_inventory_checks(
        self, mock_loader: MagicMock
    ) -> None:
        steps = [
            _make_step(
                "start",
                allowed_mcp_tools=["gobby-merge:missing_tool"],
                on_mcp_success=[
                    {
                        "server": "gobby-merge",
                        "tool": "missing_tool",
                        "action": "set_variable",
                    }
                ],
            )
        ]
        mcp_manager = MagicMock()
        mcp_manager.get_available_servers.return_value = ["gobby-merge"]
        mcp_manager.list_tools = AsyncMock(return_value={})

        result = await evaluate_agent_definition(
            _as_agent(_make_definition(steps=steps)), mcp_manager=mcp_manager
        )

        assert not [
            i for i in result.items if i.code in {"UNKNOWN_MCP_TOOL", "UNKNOWN_MCP_HANDLER_TARGET"}
        ]


class TestUnsupportedLifecycleActions:
    @pytest.mark.asyncio
    async def test_dead_action_lists_are_reported(self, mock_loader: MagicMock) -> None:
        steps = [
            _make_step(
                "start",
                on_enter=[{"type": "set_variable"}],
                on_exit=[{"type": "set_variable"}],
                transitions=[
                    {
                        "to": "done",
                        "when": "true",
                        "on_transition": [{"type": "set_variable"}],
                    }
                ],
            ),
            _make_step("done"),
        ]
        result = await evaluate_agent_definition(_as_agent(_make_definition(steps=steps)))

        fields = {
            item.detail["field"] for item in result.warnings if item.code == "ACTION_NOT_EXECUTED"
        }
        assert fields == {"on_enter", "on_exit", "on_transition"}


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
                transitions=[{"to": "report", "when": "task_tree_complete(vars.session_task)"}],
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
        result = await evaluate_agent_definition(_as_agent(definition))

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
        result = await evaluate_agent_definition(_as_agent(definition))

        assert result.lifecycle_path == ["claim_task", "work", "report", "shutdown", "complete"]

    @pytest.mark.asyncio
    async def test_lifecycle_path_includes_all_branches(self, mock_loader: MagicMock) -> None:
        """The trace includes every reachable branch in transition order."""
        steps = [
            _make_step(
                "start",
                transitions=[
                    {"to": "success", "when": "vars.ok"},
                    {"to": "failure", "when": "not vars.ok"},
                ],
            ),
            _make_step("success", transitions=[{"to": "done", "when": "true"}]),
            _make_step("failure", transitions=[{"to": "done", "when": "true"}]),
            _make_step("done"),
        ]
        result = await evaluate_agent_definition(_as_agent(_make_definition(steps=steps)))

        assert result.lifecycle_path == ["start", "success", "failure", "done"]


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
        result = await evaluate_agent_definition(_as_agent(definition))

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
        )
        d = result.to_dict()
        assert d["valid"] is True
        assert d["workflow_name"] == "test"
        assert d["items"] == []
