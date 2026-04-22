from datetime import UTC, datetime

import pytest

from gobby.hooks.event_handlers import EDIT_TOOLS, EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.integration


def test_edit_history_flow(temp_db, tmp_path) -> None:
    """Test full flow: session -> claim task -> edit -> had_edits set."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    in_repo_file = repo_root / "src" / "edited.py"
    in_repo_file.parent.mkdir(parents=True)

    # 1. Setup managers
    session_manager = SessionManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project_manager = LocalProjectManager(temp_db)
    session_var_manager = SessionVariableManager(temp_db)

    # Create project to satisfy FK
    project = project_manager.create("test-project", str(repo_root))
    project_id = project.id

    # EventHandlers needs session_storage and task_manager
    handlers = EventHandlers(
        session_storage=session_manager,
        task_manager=task_manager,
    )

    # 2. Register a session
    session = session_manager.register(
        external_id="test-session-1",
        machine_id="test-machine",
        source="gemini",
        project_id=project_id,
        title="Test Session",
    )
    assert not session.had_edits

    # 3. Create a task
    task = task_manager.create_task(
        project_id=project_id, title="Test Task", created_in_session_id=session.id
    )

    # 4. Claim the task (EventHandlers checks for claimed tasks)
    task_manager.claim_task(task.id, session.id)

    # 5. Simulate Edit Tool execution
    # Ensure tool name is in EDIT_TOOLS (case insensitive test)
    edit_tool = list(EDIT_TOOLS)[0]

    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session-1",
        source=SessionSource.GEMINI,
        timestamp=datetime.now(UTC),
        cwd=str(repo_root),
        data={"tool_name": edit_tool, "tool_input": {"file_path": str(in_repo_file)}},
        metadata={"_platform_session_id": session.id},
    )

    handlers.handle_after_tool(event)

    # 6. Verify had_edits is True
    session = session_manager.get(session.id)
    assert session.had_edits
    assert session_var_manager.get_variables(session.id)["session_edited_files"] == ["src/edited.py"]

    # 7. Verify non-edit tool doesn't trigger it (if it was false)
    # Reset session for negative test
    # (Manually unset in DB because we don't have a method to unset it)
    temp_db.execute("UPDATE sessions SET had_edits = 0 WHERE id = ?", (session.id,))
    session = session_manager.get(session.id)
    assert not session.had_edits

    event_read = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session-1",
        source=SessionSource.GEMINI,
        timestamp=datetime.now(UTC),
        data={"tool_name": "read_file"},
        metadata={"_platform_session_id": session.id},
    )
    handlers.handle_after_tool(event_read)

    session = session_manager.get(session.id)
    assert not session.had_edits


def test_edit_history_ignores_out_of_repo_paths(temp_db, tmp_path) -> None:
    """Test claimed out-of-repo edits do not set had_edits or session_edited_files."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_file = tmp_path / "outside" / "settings.json"
    outside_file.parent.mkdir(parents=True)

    session_manager = SessionManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project_manager = LocalProjectManager(temp_db)
    session_var_manager = SessionVariableManager(temp_db)

    project = project_manager.create("test-project-outside", str(repo_root))
    session = session_manager.register(
        external_id="test-session-outside",
        machine_id="test-machine",
        source="gemini",
        project_id=project.id,
        title="Out-of-Repo Edit Session",
    )
    task = task_manager.create_task(
        project_id=project.id,
        title="Out-of-Repo Task",
        created_in_session_id=session.id,
    )
    task_manager.claim_task(task.id, session.id)

    handlers = EventHandlers(session_storage=session_manager, task_manager=task_manager)
    edit_tool = list(EDIT_TOOLS)[0]
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session-outside",
        source=SessionSource.GEMINI,
        timestamp=datetime.now(UTC),
        cwd=str(repo_root),
        data={"tool_name": edit_tool, "tool_input": {"file_path": str(outside_file)}},
        metadata={"_platform_session_id": session.id},
    )

    handlers.handle_after_tool(event)

    session = session_manager.get(session.id)
    assert not session.had_edits
    assert "session_edited_files" not in session_var_manager.get_variables(session.id)


def test_edit_history_not_set_if_task_not_claimed(temp_db) -> None:
    """Test had_edits is NOT set if no task is claimed."""
    session_manager = SessionManager(temp_db)
    task_manager = LocalTaskManager(temp_db)
    project_manager = LocalProjectManager(temp_db)
    handlers = EventHandlers(session_storage=session_manager, task_manager=task_manager)

    project = project_manager.create("test-project-2", "/tmp/repo2")
    project_id = project.id

    session = session_manager.register(
        external_id="test-session-2",
        machine_id="test-machine",
        source="gemini",
        project_id=project_id,
    )

    # Create task but DON'T claim it
    task_manager.create_task(
        project_id=project_id, title="Unclaimed Task", created_in_session_id=session.id
    )

    edit_tool = list(EDIT_TOOLS)[0]
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session-2",
        source=SessionSource.GEMINI,
        timestamp=datetime.now(UTC),
        data={"tool_name": edit_tool},
        metadata={"_platform_session_id": session.id},
    )

    handlers.handle_after_tool(event)

    session = session_manager.get(session.id)
    assert not session.had_edits
