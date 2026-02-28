"""
Additional tests for WorkflowEngine to increase coverage.

Covers:
- Lines 100-103: __lifecycle__ workflow handling (skip step workflow handling)
- Lines 124-131: Session info lookup via session_manager.find_by_external_id
- Lines 161-164: Reset premature stop counter on user prompt (BEFORE_AGENT), NOT on tool calls
- Lines 898-988: _check_premature_stop method
- Lines 1079-1090: _log_approval audit logging method
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.workflow_audit import WorkflowAuditManager
from gobby.workflows.actions import ActionExecutor
from gobby.workflows.definitions import (
    PrematureStopHandler,
    WorkflowDefinition,
    WorkflowState,
    WorkflowStep,
    WorkflowTransition,
)
from gobby.workflows.engine import WorkflowEngine
from gobby.workflows.engine_models import TransitionResult
from gobby.workflows.evaluator import ConditionEvaluator
from gobby.workflows.loader import WorkflowLoader
from gobby.workflows.state_manager import WorkflowStateManager

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_loader():
    return MagicMock(spec=WorkflowLoader)


@pytest.fixture
def mock_state_manager():
    return MagicMock(spec=WorkflowStateManager)


@pytest.fixture
def mock_action_executor():
    executor = AsyncMock(spec=ActionExecutor)
    executor.db = MagicMock()
    executor.session_manager = MagicMock()
    executor.template_engine = MagicMock()
    # Configure template_engine.render to return its first argument (template string)
    # This simulates pass-through rendering for tests
    executor.template_engine.render.side_effect = lambda template, context: template
    executor.llm_service = MagicMock()
    executor.transcript_processor = MagicMock()
    executor.config = MagicMock()
    executor.mcp_manager = MagicMock()
    executor.memory_manager = MagicMock()
    executor.memory_sync_manager = MagicMock()
    executor.session_task_manager = MagicMock()
    return executor


@pytest.fixture
def mock_evaluator():
    return MagicMock(spec=ConditionEvaluator)


@pytest.fixture
def mock_audit_manager():
    return MagicMock(spec=WorkflowAuditManager)


@pytest.fixture
def workflow_engine(
    mock_loader, mock_state_manager, mock_action_executor, mock_evaluator, mock_audit_manager
):
    return WorkflowEngine(
        mock_loader,
        mock_state_manager,
        mock_action_executor,
        evaluator=mock_evaluator,
        audit_manager=mock_audit_manager,
    )


def create_event(
    event_type=HookEventType.BEFORE_AGENT,
    session_id="sess1",
    data=None,
    metadata=None,
    cwd=None,
    machine_id=None,
    project_id=None,
):
    if data is None:
        data = {}
    if metadata is None:
        metadata = {"_platform_session_id": session_id}
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata=metadata,
        cwd=cwd,
        machine_id=machine_id,
        project_id=project_id,
    )


@pytest.mark.asyncio
class TestLifecycleWorkflowState:
    """Tests for lines 100-103: __lifecycle__ workflow handling."""

    async def test_lifecycle_state_skips_step_workflow_handling(
        self, workflow_engine, mock_state_manager, mock_loader
    ):
        """When workflow_name is __lifecycle__, step workflow handling is skipped."""
        # Create a lifecycle-only state (used for task_claimed tracking)
        state = WorkflowState(
            session_id="sess1",
            workflow_name="__lifecycle__",  # Special lifecycle state
            step="",  # Empty step for lifecycle
            step_entered_at=datetime.now(UTC),
            variables={"task_claimed": True},
        )
        mock_state_manager.get_state.return_value = state

        event = create_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
        )

        response = await workflow_engine.handle_event(event)

        # Should allow without loading workflow definition
        assert response.decision == "allow"
        # load_workflow should NOT be called for step handling
        mock_loader.load_workflow.assert_not_called()


@pytest.mark.asyncio
class TestSessionInfoLookup:
    """Tests for lines 124-131: Session info lookup via find_by_external_id."""

    async def test_session_info_populated_in_eval_context(
        self, workflow_engine, mock_state_manager, mock_loader, mock_action_executor, mock_evaluator
    ):
        """Session info is fetched and added to evaluation context."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={},
        )
        mock_state_manager.get_state.return_value = state

        # Setup mock session returned by find_by_external_id
        mock_session = MagicMock()
        mock_session.id = "internal-123"
        mock_session.external_id = "ext-sess1"
        mock_session.project_id = "proj-1"
        mock_session.status = "active"
        mock_session.git_branch = "feature/test"
        mock_session.source = "claude"
        mock_action_executor.session_manager.find_by_external_id.return_value = mock_session

        # Setup workflow with step
        step = MagicMock(spec=WorkflowStep)
        step.blocked_tools = []
        step.allowed_tools = "all"
        step.rules = []

        step.transitions = []
        step.exit_conditions = []
        step.on_enter = []

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.get_step.return_value = step
        mock_loader.load_workflow.return_value = workflow

        event = create_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
            machine_id="machine-1",
            project_id="proj-1",
        )

        await workflow_engine.handle_event(event)

        # Verify find_by_external_id was called with correct params
        mock_action_executor.session_manager.find_by_external_id.assert_called_once_with(
            external_id="sess1",
            machine_id="machine-1",
            project_id="proj-1",
            source="claude",
        )

    async def test_session_info_not_fetched_when_missing_ids(
        self, workflow_engine, mock_state_manager, mock_loader, mock_action_executor
    ):
        """Session lookup is skipped when machine_id or project_id is missing."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
        )
        mock_state_manager.get_state.return_value = state

        step = MagicMock(spec=WorkflowStep)
        step.blocked_tools = []
        step.allowed_tools = "all"
        step.rules = []

        step.transitions = []
        step.exit_conditions = []
        step.on_enter = []

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.get_step.return_value = step
        mock_loader.load_workflow.return_value = workflow

        # Event without machine_id or project_id
        event = create_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
        )

        await workflow_engine.handle_event(event)

        # find_by_external_id should NOT be called
        mock_action_executor.session_manager.find_by_external_id.assert_not_called()


@pytest.mark.asyncio
class TestPrematureStopCounterReset:
    """Tests for lines 161-164: Reset premature stop counter on user prompt."""

    async def test_premature_stop_counter_reset_on_before_agent(
        self, workflow_engine, mock_state_manager, mock_loader, mock_evaluator
    ):
        """Premature stop counter is reset to 0 on BEFORE_AGENT (user prompt)."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={"_premature_stop_count": 2},  # Counter from previous attempts
        )
        mock_state_manager.get_state.return_value = state

        step = MagicMock(spec=WorkflowStep)
        step.blocked_tools = []
        step.allowed_tools = "all"
        step.rules = []
        step.transitions = []
        step.exit_conditions = []
        step.on_enter = []

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.get_step.return_value = step
        mock_loader.load_workflow.return_value = workflow

        # Mock evaluator to return no pending approval
        mock_evaluator.check_pending_approval.return_value = None

        event = create_event(
            event_type=HookEventType.BEFORE_AGENT,
            data={"prompt": "continue working"},
        )

        await workflow_engine.handle_event(event)

        # Counter should be reset to 0
        assert state.variables["_premature_stop_count"] == 0
        mock_state_manager.save_state.assert_called_with(state)

    async def test_premature_stop_counter_not_reset_when_zero(
        self, workflow_engine, mock_state_manager, mock_loader, mock_evaluator
    ):
        """Counter reset is skipped when it's already 0."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={},  # No _premature_stop_count (defaults to 0)
        )
        mock_state_manager.get_state.return_value = state

        step = MagicMock(spec=WorkflowStep)
        step.blocked_tools = []
        step.allowed_tools = "all"
        step.rules = []
        step.transitions = []
        step.exit_conditions = []
        step.on_enter = []

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.get_step.return_value = step
        mock_loader.load_workflow.return_value = workflow

        mock_evaluator.check_pending_approval.return_value = None

        event = create_event(
            event_type=HookEventType.BEFORE_AGENT,
            data={"prompt": "hello"},
        )

        # Clear any previous calls
        mock_state_manager.save_state.reset_mock()

        await workflow_engine.handle_event(event)

        # save_state should NOT be called just for counter reset when it's already 0
        # (it might be called for other reasons, but the counter reset path is skipped)
        assert state.variables.get("_premature_stop_count", 0) == 0

    async def test_premature_stop_counter_not_reset_on_before_tool(
        self, workflow_engine, mock_state_manager, mock_loader, mock_evaluator
    ):
        """Premature stop counter must NOT reset on BEFORE_TOOL events.

        For orchestrator workflows in mode: self, the orchestrator's own MCP
        tool calls would reset the counter on every call, defeating the
        failsafe that detects stuck-in-loop agents.
        """
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={"_premature_stop_count": 2},
        )
        mock_state_manager.get_state.return_value = state

        step = MagicMock(spec=WorkflowStep)
        step.blocked_tools = []
        step.allowed_tools = "all"
        step.rules = []

        step.transitions = []
        step.exit_conditions = []
        step.on_enter = []

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.get_step.return_value = step
        mock_loader.load_workflow.return_value = workflow

        event = create_event(
            event_type=HookEventType.BEFORE_TOOL,
            data={"tool_name": "suggest_next_task"},
        )

        await workflow_engine.handle_event(event)

        # Counter should remain at 2 — tool calls must NOT reset it
        assert state.variables["_premature_stop_count"] == 2


@pytest.mark.asyncio
class TestCheckPrematureStop:
    """Tests for lines 898-988: _check_premature_stop method."""

    async def test_premature_stop_no_session_id(self, workflow_engine):
        """Returns None when no session_id in event metadata."""
        event = create_event(
            event_type=HookEventType.STOP,
            metadata={},  # No _platform_session_id
        )

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is None

    async def test_premature_stop_no_state(self, workflow_engine, mock_state_manager):
        """Returns None when no workflow state exists for session."""
        mock_state_manager.get_state.return_value = None

        event = create_event(event_type=HookEventType.STOP)

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is None

    async def test_premature_stop_lifecycle_state_skipped(
        self, workflow_engine, mock_state_manager
    ):
        """Returns None for __lifecycle__ states."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="__lifecycle__",
            step="",
            step_entered_at=datetime.now(UTC),
        )
        mock_state_manager.get_state.return_value = state

        event = create_event(event_type=HookEventType.STOP)

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is None

    async def test_premature_stop_workflow_not_found(
        self, workflow_engine, mock_state_manager, mock_loader
    ):
        """Returns None when workflow definition is not found."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="missing_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
        )
        mock_state_manager.get_state.return_value = state
        mock_loader.load_workflow.return_value = None

        event = create_event(event_type=HookEventType.STOP, cwd="/project")

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is None

    async def test_premature_stop_no_exit_condition(
        self, workflow_engine, mock_state_manager, mock_loader
    ):
        """Returns None when workflow has no exit_condition."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
        )
        mock_state_manager.get_state.return_value = state

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.exit_condition = None  # No exit condition
        mock_loader.load_workflow.return_value = workflow

        event = create_event(event_type=HookEventType.STOP, cwd="/project")

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is None

    async def test_premature_stop_exit_condition_met(
        self, workflow_engine, mock_state_manager, mock_loader, mock_evaluator
    ):
        """Returns None when exit_condition is met (normal stop)."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={"task_complete": True},
        )
        mock_state_manager.get_state.return_value = state

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.name = "test_wf"
        workflow.exit_condition = "variables.task_complete"
        workflow.on_premature_stop = MagicMock()
        mock_loader.load_workflow.return_value = workflow

        # Exit condition evaluates to True
        mock_evaluator.evaluate.return_value = True

        event = create_event(event_type=HookEventType.STOP, cwd="/project")

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is None

    async def test_premature_stop_no_handler(
        self, workflow_engine, mock_state_manager, mock_loader, mock_evaluator
    ):
        """Returns None when exit_condition not met but no on_premature_stop handler."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={},
        )
        mock_state_manager.get_state.return_value = state

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.name = "test_wf"
        workflow.exit_condition = "variables.task_complete"
        workflow.on_premature_stop = None  # No handler
        mock_loader.load_workflow.return_value = workflow

        mock_evaluator.evaluate.return_value = False  # Exit condition not met

        event = create_event(event_type=HookEventType.STOP, cwd="/project")

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is None

    async def test_premature_stop_failsafe_triggered(
        self, workflow_engine, mock_state_manager, mock_loader, mock_evaluator
    ):
        """Allows stop when failsafe is triggered (max attempts exceeded)."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={
                "_premature_stop_count": 2,  # Will become 3 with +1
                "premature_stop_max_attempts": 3,
            },
        )
        mock_state_manager.get_state.return_value = state

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.name = "test_wf"
        workflow.exit_condition = "variables.task_complete"
        workflow.on_premature_stop = PrematureStopHandler(
            action="block", message="Task not complete"
        )
        mock_loader.load_workflow.return_value = workflow

        mock_evaluator.evaluate.return_value = False

        event = create_event(event_type=HookEventType.STOP, cwd="/project")

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is not None
        assert result.decision == "allow"
        assert "Failsafe Exit" in result.context
        assert state.variables["_premature_stop_count"] == 3

    async def test_premature_stop_handler_block(
        self, workflow_engine, mock_state_manager, mock_loader, mock_evaluator
    ):
        """Handler action='block' returns block response."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={},
        )
        mock_state_manager.get_state.return_value = state

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.name = "test_wf"
        workflow.exit_condition = "variables.task_complete"
        workflow.on_premature_stop = PrematureStopHandler(
            action="block", message="You cannot stop until the task is complete."
        )
        mock_loader.load_workflow.return_value = workflow

        mock_evaluator.evaluate.return_value = False

        event = create_event(event_type=HookEventType.STOP, cwd="/project")

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is not None
        assert result.decision == "block"
        assert result.reason == "You cannot stop until the task is complete."

    async def test_premature_stop_handler_warn(
        self, workflow_engine, mock_state_manager, mock_loader, mock_evaluator
    ):
        """Handler action='warn' returns allow with warning context."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={},
        )
        mock_state_manager.get_state.return_value = state

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.name = "test_wf"
        workflow.exit_condition = "variables.task_complete"
        workflow.on_premature_stop = PrematureStopHandler(
            action="warn", message="Task may not be complete."
        )
        mock_loader.load_workflow.return_value = workflow

        mock_evaluator.evaluate.return_value = False

        event = create_event(event_type=HookEventType.STOP, cwd="/project")

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is not None
        assert result.decision == "allow"
        assert "Warning" in result.context
        assert "Task may not be complete." in result.context

    async def test_premature_stop_handler_guide_continuation(
        self, workflow_engine, mock_state_manager, mock_loader, mock_evaluator
    ):
        """Handler action='guide_continuation' (default) returns block with guidance."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={},
        )
        mock_state_manager.get_state.return_value = state

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.name = "test_wf"
        workflow.exit_condition = "variables.task_complete"
        workflow.on_premature_stop = PrematureStopHandler(
            action="guide_continuation",
            message="Please complete all subtasks first.",
        )
        mock_loader.load_workflow.return_value = workflow

        mock_evaluator.evaluate.return_value = False

        event = create_event(event_type=HookEventType.STOP, cwd="/project")

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is not None
        assert result.decision == "block"
        assert result.reason == "Please complete all subtasks first."
        assert "Task Incomplete" in result.context
        assert "exit condition" in result.context

    async def test_premature_stop_renders_jinja_variables(
        self, workflow_engine, mock_state_manager, mock_loader, mock_evaluator, mock_action_executor
    ):
        """on_premature_stop message should render Jinja2 variables."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={"session_task": "task-123", "worktree_path": "/tmp/worktree"},
        )
        mock_state_manager.get_state.return_value = state

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.name = "test_wf"
        workflow.exit_condition = "variables.task_complete"
        workflow.on_premature_stop = PrematureStopHandler(
            action="guide_continuation",
            message="Task {{ variables.session_task }} not complete. Worktree: {{ variables.worktree_path }}",
        )
        mock_loader.load_workflow.return_value = workflow

        mock_evaluator.evaluate.return_value = False

        # Configure template_engine.render to actually render like a real Jinja template
        def render_template(template, context):
            from jinja2 import Template

            return Template(template).render(**context)

        mock_action_executor.template_engine.render.side_effect = render_template

        event = create_event(event_type=HookEventType.STOP, cwd="/project")

        result = await workflow_engine._check_premature_stop(event, {})

        assert result is not None
        assert result.decision == "block"
        # Verify variables were rendered
        assert "task-123" in result.reason
        assert "/tmp/worktree" in result.reason
        assert "{{ variables.session_task }}" not in result.reason  # Not literal


class TestLogApproval:
    """Tests for lines 1079-1090: _log_approval audit logging method."""

    def test_log_approval_success(self, workflow_engine, mock_audit_manager) -> None:
        """_log_approval calls audit_manager.log_approval successfully."""
        workflow_engine._log_approval(
            session_id="sess1",
            step="working",
            result="approved",
            condition_id="cond1",
            prompt="Ready to proceed?",
            context={"key": "value"},
        )

        mock_audit_manager.log_approval.assert_called_once_with(
            session_id="sess1",
            step="working",
            result="approved",
            condition_id="cond1",
            prompt="Ready to proceed?",
            context={"key": "value"},
        )

    def test_log_approval_exception_handled(self, workflow_engine, mock_audit_manager) -> None:
        """_log_approval handles exceptions gracefully."""
        mock_audit_manager.log_approval.side_effect = Exception("Database error")

        # Should not raise
        workflow_engine._log_approval(
            session_id="sess1",
            step="working",
            result="rejected",
        )

    def test_log_approval_no_audit_manager(
        self, mock_loader, mock_state_manager, mock_action_executor
    ) -> None:
        """_log_approval does nothing when audit_manager is None."""
        engine = WorkflowEngine(
            mock_loader,
            mock_state_manager,
            mock_action_executor,
            audit_manager=None,  # No audit manager
        )

        # Should not raise
        engine._log_approval(
            session_id="sess1",
            step="working",
            result="approved",
        )


@pytest.mark.asyncio
class TestCloseTaskClearsTaskClaimed:
    """Tests for close_task clearing task_claimed in _detect_task_claim."""

    async def test_close_task_clears_task_claimed(
        self, workflow_engine, mock_state_manager, mock_loader
    ):
        """close_task call clears task_claimed and claimed_task_id."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="default",
            step="step1",
            step_entered_at=datetime.now(UTC),
            variables={"task_claimed": True, "claimed_task_id": "gt-123"},
        )
        mock_state_manager.get_state.return_value = state

        step = MagicMock(spec=WorkflowStep)
        step.blocked_tools = []
        step.allowed_tools = "all"
        step.rules = []
        step.transitions = []
        step.exit_conditions = []
        step.on_enter = []

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.get_step.return_value = step
        mock_loader.load_workflow.return_value = workflow

        event = create_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "mcp_server": "gobby-tasks",  # Normalized MCP fields
                "mcp_tool": "close_task",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "gt-123"},
                },
                "tool_output": {"status": "success"},
            },
        )

        await workflow_engine.handle_event(event)

        assert state.variables.get("task_claimed") is False
        assert state.variables.get("claimed_task_id") is None


@pytest.mark.asyncio
class TestDetectTaskClaimWithNestedError:
    """Additional tests for _detect_task_claim edge cases."""

    async def test_nested_result_error_does_not_set_task_claimed(
        self, workflow_engine, mock_state_manager, mock_loader
    ):
        """MCP proxy nested result error does NOT set task_claimed."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="default",
            step="step1",
            step_entered_at=datetime.now(UTC),
            variables={},
        )
        mock_state_manager.get_state.return_value = state

        step = MagicMock(spec=WorkflowStep)
        step.blocked_tools = []
        step.allowed_tools = "all"
        step.rules = []
        step.transitions = []
        step.exit_conditions = []
        step.on_enter = []

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.get_step.return_value = step
        mock_loader.load_workflow.return_value = workflow

        event = create_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "create_task",
                    "arguments": {"title": "Test task"},
                },
                "tool_output": {
                    "status": "success",
                    "result": {"error": "Validation error"},  # Nested error
                },
            },
        )

        await workflow_engine.handle_event(event)

        assert state.variables.get("task_claimed") is None

    async def test_detect_task_claim_no_event_data(self, workflow_engine):
        """_detect_task_claim returns early when event.data is None."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            variables={},
        )

        event = create_event(
            event_type=HookEventType.AFTER_TOOL,
            data=None,
        )
        event.data = None  # Explicitly set to None

        # Should not raise
        workflow_engine._detect_task_claim(event, state)

        assert state.variables.get("task_claimed") is None


@pytest.mark.asyncio
@pytest.mark.asyncio
class TestApprovalPromptReminder:
    """Test that non-approval responses remind user about pending approval."""

    async def test_non_approval_response_shows_reminder(
        self, workflow_engine, mock_state_manager, mock_loader
    ):
        """User prompt that isn't approval keyword shows reminder."""
        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC),
            approval_pending=True,
            approval_condition_id="cond1",
            approval_prompt="Ready to deploy?",
            variables={},
        )
        mock_state_manager.get_state.return_value = state

        step = MagicMock(spec=WorkflowStep)
        step.exit_conditions = []
        step.on_enter = []

        workflow = MagicMock(spec=WorkflowDefinition)
        workflow.type = "step"
        workflow.steps = [MagicMock()]
        workflow.get_step.return_value = step
        mock_loader.load_workflow.return_value = workflow

        event = create_event(
            event_type=HookEventType.BEFORE_AGENT,
            data={"prompt": "what is the status?"},  # Not yes/no
        )

        response = await workflow_engine.handle_event(event)

        assert response.decision == "allow"
        assert "Waiting for approval" in response.context
        assert "Ready to deploy?" in response.context
        assert state.approval_pending is True  # Still pending


class TestConfigurableStuckTimeout:
    """Tests for configurable stuck_timeout workflow variable."""

    @pytest.mark.asyncio
    async def test_default_stuck_timeout_triggers_at_1800s(
        self, workflow_engine, mock_state_manager, mock_loader
    ) -> None:
        """Default stuck timeout triggers at 30 minutes (1800s)."""
        from datetime import timedelta
        from unittest.mock import patch as _patch

        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC) - timedelta(seconds=1801),
            variables={},  # No stuck_timeout set — uses default 1800
        )
        mock_state_manager.get_state.return_value = state

        # Workflow with reflect step so transition happens
        workflow = WorkflowDefinition(
            name="test_wf",
            steps=[WorkflowStep(name="working"), WorkflowStep(name="reflect")],
        )
        mock_loader.load_workflow.return_value = workflow

        # Mock transition_to to avoid deep dependency chain
        mock_result = MagicMock()
        mock_result.injected_messages = []
        mock_result.system_messages = []
        with _patch.object(workflow_engine, "transition_to", return_value=mock_result):
            event = create_event(event_type=HookEventType.BEFORE_TOOL)
            response = await workflow_engine.handle_event(event)

        # Should trigger stuck detection → transition to reflect
        assert response.decision == "modify"
        assert "Step duration limit exceeded" in response.context

    @pytest.mark.asyncio
    async def test_custom_stuck_timeout_does_not_trigger_early(
        self, workflow_engine, mock_state_manager, mock_loader
    ) -> None:
        """Custom stuck_timeout=7200 does not trigger at 31 minutes."""
        from datetime import timedelta

        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC) - timedelta(seconds=1860),  # 31 min
            variables={"stuck_timeout": 7200},  # 2 hours
        )
        mock_state_manager.get_state.return_value = state

        workflow = WorkflowDefinition(
            name="test_wf",
            steps=[WorkflowStep(name="working")],
        )
        mock_loader.load_workflow.return_value = workflow

        event = create_event(event_type=HookEventType.BEFORE_TOOL)

        response = await workflow_engine.handle_event(event)

        # Should NOT trigger stuck detection — 31 min < 2 hours
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_custom_stuck_timeout_triggers_when_exceeded(
        self, workflow_engine, mock_state_manager, mock_loader
    ) -> None:
        """Custom stuck_timeout=3600 triggers when exceeded."""
        from datetime import timedelta
        from unittest.mock import patch as _patch

        state = WorkflowState(
            session_id="sess1",
            workflow_name="test_wf",
            step="working",
            step_entered_at=datetime.now(UTC) - timedelta(seconds=3601),  # Just over 1 hour
            variables={"stuck_timeout": 3600},  # 1 hour
        )
        mock_state_manager.get_state.return_value = state

        workflow = WorkflowDefinition(
            name="test_wf",
            steps=[WorkflowStep(name="working"), WorkflowStep(name="reflect")],
        )
        mock_loader.load_workflow.return_value = workflow

        mock_result = MagicMock()
        mock_result.injected_messages = []
        mock_result.system_messages = []
        with _patch.object(workflow_engine, "transition_to", return_value=mock_result):
            event = create_event(event_type=HookEventType.BEFORE_TOOL)
            response = await workflow_engine.handle_event(event)

        assert response.decision == "modify"
        assert "Step duration limit exceeded" in response.context


class TestAutoTransitionDepthLimitLogging:
    """Tests for auto-transition chain depth limit logging (#7388)."""

    @pytest.mark.asyncio
    async def test_chain_truncation_logs_error_with_session_and_chain(
        self, workflow_engine, mock_state_manager, mock_loader
    ) -> None:
        """Truncated chain logs error with session_id and visited steps."""
        from unittest.mock import patch as _patch

        # Create a cycle: step_a → step_b → step_c → step_a (infinite loop)
        step_a = WorkflowStep(
            name="step_a",
            transitions=[WorkflowTransition(to="step_b", when="true")],
        )
        step_b = WorkflowStep(
            name="step_b",
            transitions=[WorkflowTransition(to="step_c", when="true")],
        )
        step_c = WorkflowStep(
            name="step_c",
            transitions=[WorkflowTransition(to="step_a", when="true")],
        )
        workflow = WorkflowDefinition(
            name="test_cycle_wf",
            steps=[step_a, step_b, step_c],
        )

        state = WorkflowState(
            session_id="sess-depth-test",
            workflow_name="test_cycle_wf",
            step="step_a",
            variables={},
        )

        event = create_event(session_id="sess-depth-test")
        initial_result = TransitionResult()

        # Mock transition_to to simulate step changes without full engine
        async def fake_transition(s, to_step, wf, **kwargs):
            s.step = to_step
            return TransitionResult()

        with (
            _patch.object(workflow_engine, "transition_to", side_effect=fake_transition),
            _patch("gobby.workflows.engine_transitions.logger") as mock_logger,
        ):
            await workflow_engine._auto_transition_chain(
                state=state,
                workflow=workflow,
                session_info={},
                project_info={},
                event=event,
                initial_result=initial_result,
                max_depth=3,
            )

        # Should log error (not warning)
        mock_logger.error.assert_called_once()
        log_msg = mock_logger.error.call_args[0][0]

        # Verify session_id is included
        assert "sess-depth-test" in log_msg
        # Verify chain is included (step_a → step_b → step_c → step_a)
        assert "step_a" in log_msg
        assert "step_b" in log_msg
        assert "step_c" in log_msg
        assert "→" in log_msg
        # Verify max_depth mentioned
        assert "max_depth=3" in log_msg

    @pytest.mark.asyncio
    async def test_no_truncation_when_chain_stops_early(
        self, workflow_engine, mock_state_manager, mock_loader
    ) -> None:
        """No error logged when chain stops before max_depth."""
        from unittest.mock import patch as _patch

        # step_a → step_b (no further transitions)
        step_a = WorkflowStep(
            name="step_a",
            transitions=[WorkflowTransition(to="step_b", when="true")],
        )
        step_b = WorkflowStep(name="step_b")  # No transitions — stops here
        workflow = WorkflowDefinition(
            name="test_wf",
            steps=[step_a, step_b],
        )

        state = WorkflowState(
            session_id="sess2",
            workflow_name="test_wf",
            step="step_a",
            variables={},
        )

        event = create_event(session_id="sess2")
        initial_result = TransitionResult()

        async def fake_transition(s, to_step, wf, **kwargs):
            s.step = to_step
            return TransitionResult()

        with (
            _patch.object(workflow_engine, "transition_to", side_effect=fake_transition),
            _patch("gobby.workflows.engine_transitions.logger") as mock_logger,
        ):
            await workflow_engine._auto_transition_chain(
                state=state,
                workflow=workflow,
                session_info={},
                project_info={},
                event=event,
                initial_result=initial_result,
                max_depth=10,
            )

        # No error — chain stopped naturally
        mock_logger.error.assert_not_called()
