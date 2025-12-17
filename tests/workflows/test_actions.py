import pytest
from unittest.mock import MagicMock, AsyncMock
from gobby.workflows.actions import ActionExecutor, ActionContext
from gobby.workflows.definitions import WorkflowState
from gobby.storage.sessions import Session
from datetime import datetime, UTC


@pytest.fixture
def action_executor(temp_db, session_manager):
    return ActionExecutor(temp_db, session_manager)


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
async def test_generate_handoff(action_executor, action_context, session_manager, sample_project):
    # Setup session
    session = session_manager.register(
        external_id="handoff-ext",
        machine_id="test-machine",
        source="test-source",
        project_id=sample_project["id"],
    )
    action_context.session_id = session.id

    # Add some artifacts
    action_context.state.artifacts["plan"] = "/path/to/plan.md"

    result = await action_executor.execute(
        "generate_handoff", action_context, include=["artifacts"]
    )

    assert result is not None
    assert result["handoff_created"] is True

    # Verify DB record
    row = action_context.db.fetchone(
        "SELECT * FROM workflow_handoffs WHERE from_session_id = ?", (session.id,)
    )
    assert row is not None
    assert row["workflow_name"] == "test-workflow"
    assert "plan.md" in row["artifacts"]

    # Verify session status
    updated_session = session_manager.get(session.id)
    assert updated_session.status == "handoff_ready"
