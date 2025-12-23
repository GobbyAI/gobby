import pytest
from unittest.mock import MagicMock, ANY
from gobby.workflows.actions import ActionExecutor, ActionContext
from gobby.workflows.definitions import WorkflowState


@pytest.fixture
def mock_context():
    context = MagicMock(spec=ActionContext)
    context.session_id = "test-session"
    context.session_manager = MagicMock()
    context.template_engine = MagicMock()
    context.template_engine.render.side_effect = lambda t, c: t  # specific render mock if needed

    # Setup state
    context.state = WorkflowState(
        session_id="test-session", workflow_name="test-workflow", phase="test-phase"
    )
    return context


@pytest.mark.asyncio
async def test_inject_context_previous_session_summary(mock_context):
    executor = ActionExecutor(
        db=MagicMock(),
        session_manager=mock_context.session_manager,
        template_engine=mock_context.template_engine,
    )

    # Mock current session with parent
    current_session = MagicMock()
    current_session.parent_session_id = "parent-123"
    mock_context.session_manager.get.side_effect = (
        lambda sid: current_session if sid == "test-session" else parent_session
    )

    # Mock parent session with summary
    parent_session = MagicMock()
    parent_session.summary_markdown = "Summary of previous session"

    # Execute action
    result = await executor.execute(
        "inject_context", mock_context, source="previous_session_summary"
    )

    assert result == {"inject_context": "Summary of previous session"}
    assert mock_context.state.context_injected is True


@pytest.mark.asyncio
async def test_inject_context_artifacts(mock_context):
    executor = ActionExecutor(
        db=MagicMock(),
        session_manager=mock_context.session_manager,
        template_engine=mock_context.template_engine,
    )

    # Setup artifacts in state
    mock_context.state.artifacts = {
        "plan.md": "/abs/path/to/plan.md",
        "spec.md": "/abs/path/to/spec.md",
    }

    result = await executor.execute("inject_context", mock_context, source="artifacts")

    expected_content = (
        "## Captured Artifacts\n- plan.md: /abs/path/to/plan.md\n- spec.md: /abs/path/to/spec.md"
    )
    assert result["inject_context"] == expected_content


@pytest.mark.asyncio
async def test_inject_context_observations(mock_context):
    executor = ActionExecutor(
        db=MagicMock(),
        session_manager=mock_context.session_manager,
        template_engine=mock_context.template_engine,
    )

    # Setup observations
    mock_context.state.observations = [{"tool": "read_file", "result": "content"}]

    result = await executor.execute("inject_context", mock_context, source="observations")

    assert "## Observations" in result["inject_context"]
    assert '"tool": "read_file"' in result["inject_context"]


@pytest.mark.asyncio
async def test_inject_context_workflow_state(mock_context):
    executor = ActionExecutor(
        db=MagicMock(),
        session_manager=mock_context.session_manager,
        template_engine=mock_context.template_engine,
    )

    result = await executor.execute("inject_context", mock_context, source="workflow_state")

    assert "## Workflow State" in result["inject_context"]
    assert '"workflow_name": "test-workflow"' in result["inject_context"]
