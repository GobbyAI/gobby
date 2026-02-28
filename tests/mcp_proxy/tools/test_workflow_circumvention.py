"""Tests for workflow circumvention prevention.

These tests verify that agents cannot bypass workflow controls:
1. Manual transitions to steps with conditional auto-transitions are blocked
2. Modifying session_task when a real workflow is active is blocked
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.workflows import create_workflows_registry
from gobby.workflows.definitions import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowStep,
    WorkflowTransition,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_db():
    """Create a mock database."""
    return MagicMock()


@pytest.fixture
def mock_state_manager():
    """Create a mock workflow state manager."""
    return MagicMock()


@pytest.fixture
def mock_session_var_manager():
    """Create a mock session variable manager."""
    manager = MagicMock()
    # Simple in-memory variables store for tests
    store = {}

    def set_var(session_id, name, value):
        if session_id not in store:
            store[session_id] = {}
        store[session_id][name] = value

    def get_vars(session_id):
        return store.get(session_id, {})

    manager.set_variable.side_effect = set_var
    manager.get_variables.side_effect = get_vars
    return manager


@pytest.fixture
def mock_session_manager():
    """Create a mock session manager."""
    manager = MagicMock()
    # Make resolve_session_reference return the input unchanged (for testing)
    manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
    return manager


@pytest.fixture
def mock_loader():
    """Create a mock workflow loader with async load_workflow."""
    loader = MagicMock()
    loader.global_dirs = []
    loader.load_workflow = AsyncMock()
    return loader


@pytest.fixture
def registry(
    mock_loader, mock_state_manager, mock_session_manager, mock_db, mock_session_var_manager
):
    """Create workflow registry for testing."""
    with (
        patch("gobby.mcp_proxy.tools.workflows.WorkflowInstanceManager", return_value=None),
        patch(
            "gobby.mcp_proxy.tools.workflows.SessionVariableManager",
            return_value=mock_session_var_manager,
        ),
        patch(
            "gobby.workflows.state_manager.SessionVariableManager",
            return_value=mock_session_var_manager,
        ),
    ):
        return create_workflows_registry(
            loader=mock_loader,
            state_manager=mock_state_manager,
            session_manager=mock_session_manager,
            db=mock_db,
        )


def call_tool(registry: Any, tool_name: str, **kwargs: Any) -> Any:
    """Helper to call a tool from the registry."""
    tool = registry._tools.get(tool_name)
    if not tool:
        raise ValueError(f"Tool '{tool_name}' not found")
    return tool.func(**kwargs)


class TestBlockManualTransitionToConditionalSteps:
    """Tests for blocking manual transitions to steps with conditional auto-transitions."""

    @pytest.mark.asyncio
    async def test_blocks_manual_transition_to_conditional_step(
        self, registry, mock_loader, mock_state_manager
    ) -> None:
        """Manual transition to step with conditional auto-transition is blocked."""
        # Setup mock state in "work" step
        mock_state = MagicMock()
        mock_state.workflow_name = "auto-task"
        mock_state.step = "work"
        mock_state_manager.get_state.return_value = mock_state

        # Setup real workflow with work -> complete conditional transition
        work_step = WorkflowStep(
            name="work",
            transitions=[
                WorkflowTransition(
                    to="complete",
                    when="task_tree_complete(variables.session_task)",
                )
            ],
        )
        complete_step = WorkflowStep(name="complete")

        workflow = WorkflowDefinition(
            name="auto-task",
            steps=[work_step, complete_step],
        )
        mock_loader.load_workflow.return_value = workflow

        # Try to manually transition to "complete"
        result = await call_tool(
            registry,
            "request_step_transition",
            to_step="complete",
            session_id="test-session",
        )

        assert "error" in result
        assert "conditional auto-transition" in result["error"]
        assert "task_tree_complete" in result["error"]
        assert "workflow circumvention" in result["error"]

    @pytest.mark.asyncio
    async def test_allows_manual_transition_without_condition(
        self, registry, mock_loader, mock_state_manager
    ) -> None:
        """Manual transition to step without conditional auto-transition is allowed."""
        # Setup mock state
        mock_state = MagicMock()
        mock_state.workflow_name = "plan-execute"
        mock_state.step = "plan"
        mock_state_manager.get_state.return_value = mock_state

        # Setup real workflow without conditional transitions
        plan_step = WorkflowStep(name="plan")  # No transitions
        execute_step = WorkflowStep(name="execute")

        workflow = WorkflowDefinition(
            name="plan-execute",
            steps=[plan_step, execute_step],
        )
        mock_loader.load_workflow.return_value = workflow

        # Manual transition should work
        result = await call_tool(
            registry,
            "request_step_transition",
            to_step="execute",
            session_id="test-session",
        )

        assert "error" not in result
        assert result["to_step"] == "execute"

    @pytest.mark.asyncio
    async def test_allows_transition_to_step_with_unconditional_transition(
        self, registry, mock_loader, mock_state_manager
    ) -> None:
        """Manual transition is allowed when transition has no 'when' condition."""
        mock_state = MagicMock()
        mock_state.workflow_name = "test-workflow"
        mock_state.step = "step1"
        mock_state_manager.get_state.return_value = mock_state

        # Transition with empty 'when' (unconditional - always allowed)
        step1 = WorkflowStep(
            name="step1",
            transitions=[
                WorkflowTransition(to="step2", when="")  # Empty = unconditional
            ],
        )
        step2 = WorkflowStep(name="step2")

        workflow = WorkflowDefinition(
            name="test-workflow",
            steps=[step1, step2],
        )
        mock_loader.load_workflow.return_value = workflow

        result = await call_tool(
            registry,
            "request_step_transition",
            to_step="step2",
            session_id="test-session",
        )

        assert "error" not in result

    @pytest.mark.asyncio
    async def test_blocks_only_transitions_to_conditional_targets(
        self, registry, mock_loader, mock_state_manager
    ) -> None:
        """Transition to a different step (not the conditional target) is allowed."""
        mock_state = MagicMock()
        mock_state.workflow_name = "multi-step"
        mock_state.step = "step1"
        mock_state_manager.get_state.return_value = mock_state

        # step1 has conditional transition to step3, but we try to go to step2
        step1 = WorkflowStep(
            name="step1",
            transitions=[
                WorkflowTransition(to="step3", when="some_condition()")  # Conditional to step3
            ],
        )
        step2 = WorkflowStep(name="step2")
        step3 = WorkflowStep(name="step3")

        workflow = WorkflowDefinition(
            name="multi-step",
            steps=[step1, step2, step3],
        )
        mock_loader.load_workflow.return_value = workflow

        # Transition to step2 (not step3) should work
        result = await call_tool(
            registry,
            "request_step_transition",
            to_step="step2",
            session_id="test-session",
        )

        assert "error" not in result
        assert result["to_step"] == "step2"


class TestSetVariableBooleanCoercion:
    """Tests for string-to-boolean coercion in set_variable.

    MCP schema collapses union types to 'string', so agents send "true"/"false"
    as strings. Without coercion, the string "false" is truthy and breaks
    workflow gate conditions like pending_memory_review.
    """

    def test_coerces_string_false_to_bool(self, registry, mock_session_var_manager) -> None:
        """String 'false' is coerced to boolean False."""
        mock_session_var_manager.set_variable("test-session", "pending_memory_review", True)

        call_tool(
            registry,
            "set_variable",
            name="pending_memory_review",
            value="false",
            session_id="test-session",
        )

        assert (
            mock_session_var_manager.get_variables("test-session")["pending_memory_review"] is False
        )

    def test_coerces_string_true_to_bool(self, registry, mock_session_var_manager) -> None:
        """String 'true' is coerced to boolean True."""
        call_tool(
            registry,
            "set_variable",
            name="pre_existing_errors_triaged",
            value="true",
            session_id="test-session",
        )

        assert (
            mock_session_var_manager.get_variables("test-session")["pre_existing_errors_triaged"]
            is True
        )

    def test_coerces_case_insensitive(self, registry, mock_session_var_manager) -> None:
        """Coercion is case-insensitive for True/False/NULL."""
        call_tool(registry, "set_variable", name="v1", value="True", session_id="test-session")
        assert mock_session_var_manager.get_variables("test-session")["v1"] is True

        call_tool(registry, "set_variable", name="v2", value="FALSE", session_id="test-session")
        assert mock_session_var_manager.get_variables("test-session")["v2"] is False

        call_tool(registry, "set_variable", name="v3", value="Null", session_id="test-session")
        assert mock_session_var_manager.get_variables("test-session")["v3"] is None

    def test_coerces_string_null_to_none(self, registry, mock_session_var_manager) -> None:
        """String 'null' and 'none' are coerced to None."""
        call_tool(registry, "set_variable", name="v1", value="null", session_id="test-session")
        assert mock_session_var_manager.get_variables("test-session")["v1"] is None

        call_tool(registry, "set_variable", name="v2", value="none", session_id="test-session")
        assert mock_session_var_manager.get_variables("test-session")["v2"] is None

    def test_coerces_string_int_to_int(self, registry, mock_session_var_manager) -> None:
        """String '0' is coerced to integer 0."""
        call_tool(registry, "set_variable", name="count", value="0", session_id="test-session")
        assert mock_session_var_manager.get_variables("test-session")["count"] == 0
        assert isinstance(mock_session_var_manager.get_variables("test-session")["count"], int)

        call_tool(registry, "set_variable", name="count", value="42", session_id="test-session")
        assert mock_session_var_manager.get_variables("test-session")["count"] == 42

    def test_coerces_string_float_to_float(self, registry, mock_session_var_manager) -> None:
        """String '3.14' is coerced to float."""
        call_tool(registry, "set_variable", name="ratio", value="3.14", session_id="test-session")
        assert mock_session_var_manager.get_variables("test-session")["ratio"] == 3.14
        assert isinstance(mock_session_var_manager.get_variables("test-session")["ratio"], float)

    def test_preserves_regular_strings(self, registry, mock_session_var_manager) -> None:
        """Non-boolean/numeric strings are kept as-is."""
        call_tool(
            registry, "set_variable", name="name", value="hello world", session_id="test-session"
        )
        assert mock_session_var_manager.get_variables("test-session")["name"] == "hello world"

    def test_preserves_native_bool(self, registry, mock_session_var_manager) -> None:
        """Native boolean values pass through without coercion."""
        call_tool(registry, "set_variable", name="flag", value=False, session_id="test-session")
        assert mock_session_var_manager.get_variables("test-session")["flag"] is False
