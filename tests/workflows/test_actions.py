import pytest
from unittest.mock import MagicMock, AsyncMock
from gobby.workflows.actions import ActionExecutor, ActionContext
from gobby.workflows.definitions import WorkflowState
from gobby.workflows.templates import TemplateEngine
from gobby.storage.sessions import Session
from datetime import datetime, UTC


@pytest.fixture
def mock_services():
    return {
        "template_engine": MagicMock(spec=TemplateEngine),
        "llm_service": AsyncMock(),
        "transcript_processor": MagicMock(),
        "config": MagicMock(),
    }


@pytest.fixture
def action_executor(temp_db, session_manager, mock_services):
    return ActionExecutor(
        temp_db,
        session_manager,
        mock_services["template_engine"],
        llm_service=mock_services["llm_service"],
        transcript_processor=mock_services["transcript_processor"],
        config=mock_services["config"],
    )


@pytest.fixture
def workflow_state():
    return WorkflowState(
        session_id="test-session-id", workflow_name="test-workflow", phase="test-phase"
    )


@pytest.fixture
def action_context(temp_db, session_manager, workflow_state):
    return ActionContext(
        session_id=workflow_state.session_id,
        state=workflow_state,
        db=temp_db,
        session_manager=session_manager,
        template_engine=MagicMock(spec=TemplateEngine),
    )


@pytest.mark.asyncio
async def test_inject_context_previous_session(
    action_executor, action_context, session_manager, sample_project
):
    # Setup: Create parent and current session
    parent = session_manager.register(
        external_id="parent-ext",
        machine_id="test-machine",
        source="test-source",
        project_id=sample_project["id"],
        title="Parent Session",
    )
    # Update parent summary
    session_manager.update_summary(parent.id, summary_markdown="Parent Summary Content")

    current = session_manager.register(
        external_id="current-ext",
        machine_id="test-machine",
        source="test-source",
        project_id=sample_project["id"],
        title="Current Session",
        parent_session_id=parent.id,
    )

    # Update context with real session ID
    action_context.session_id = current.id
    action_context.state.session_id = current.id

    result = await action_executor.execute(
        "inject_context", action_context, source="previous_session_summary"
    )

    assert result is not None
    assert result["inject_context"] == "Parent Summary Content"
    assert action_context.state.context_injected is True


@pytest.mark.asyncio
async def test_capture_artifact(action_executor, action_context, tmp_path):
    # Create a dummy file
    artifact_file = tmp_path / "plan.md"
    artifact_file.write_text("Plan content")

    # We need to use glob pattern relative to CWD, or absolute.
    # Use absolute for test stability.
    pattern = str(artifact_file)

    result = await action_executor.execute(
        "capture_artifact",
        action_context,
        pattern=pattern,
        **{"as": "current_plan"},  # 'as' is a python keyword
    )

    assert result is not None
    assert result["captured"] == str(artifact_file)
    assert "current_plan" in action_context.state.artifacts
    assert action_context.state.artifacts["current_plan"] == str(artifact_file)


@pytest.mark.asyncio
async def test_generate_handoff(
    action_executor, action_context, session_manager, sample_project, mock_services
):
    # Setup session
    session = session_manager.register(
        external_id="handoff-ext",
        machine_id="test-machine",
        source="test-source",
        project_id=sample_project["id"],
        jsonl_path="/path/to/transcript.jsonl",
    )
    action_context.session_id = session.id

    # Setup mocks
    transcript = [{"role": "user", "content": "hello"}]
    mock_services["transcript_processor"].parse.return_value = transcript
    mock_services["transcript_processor"].format_for_llm.return_value = "User: hello"
    mock_services["template_engine"].render.return_value = "Summarize: User: hello"
    mock_services["llm_service"].generate.return_value = "Session Summary."

    # Update context services manually since ActionContext fixture doesn't use the mock_services
    # In a real app, engine handles this. Here we manually patch.
    action_context.llm_service = mock_services["llm_service"]
    action_context.transcript_processor = mock_services["transcript_processor"]
    action_context.template_engine = mock_services["template_engine"]

    result = await action_executor.execute("generate_handoff", action_context)

    assert result is not None
    assert result["handoff_created"] is True
    assert result["summary_length"] == 16

    # Verify session summary updated
    updated_session = session_manager.get(session.id)
    assert updated_session.summary_markdown == "Session Summary."
    assert updated_session.status == "handoff_ready"

    # Verify LLM called
    mock_services["llm_service"].generate.assert_called_once()
