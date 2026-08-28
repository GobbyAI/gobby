from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.session_materialize import activate_deferred_session
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import Task

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_hook_manager(temp_dir: Path, hub_db: HubDatabase) -> Iterator[HookManager]:
    """Create a HookManager with a real test database but mocked external dependencies.

    Uses a real PostgreSQL database (like hook_manager_with_mocks) to avoid 'file is not
    a database' errors from incomplete HubDatabase patching.
    """
    db = hub_db

    # Create a test project for project_id resolution
    project_mgr = LocalProjectManager(db)
    project = project_mgr.create(name="test-project", repo_path=str(temp_dir))

    # Create project.json for auto-discovery
    gobby_dir = temp_dir / ".gobby"
    gobby_dir.mkdir(exist_ok=True)
    (gobby_dir / "project.json").write_text(f'{{"id": "{project.id}", "name": "test-project"}}')

    from gobby.config.app import DaemonConfig
    from gobby.config.extensions import HookExtensionsConfig, WebhooksConfig

    # Create config with disabled external services.
    test_config = DaemonConfig(
        hook_extensions=HookExtensionsConfig(
            webhooks=WebhooksConfig(enabled=False),
        ),
    )

    with patch("gobby.hooks.factory.DaemonClient") as MockDaemonClient:
        mock_daemon_client = MagicMock()
        mock_daemon_client.check_status.return_value = (True, "Daemon ready", "ready", None)
        MockDaemonClient.return_value = mock_daemon_client

        manager = HookManager(
            daemon_host="localhost",
            daemon_port=60887,
            config=test_config,
            database=db,
        )

        # Pre-warm the daemon status cache
        manager._health_monitor._cached_daemon_is_ready = True
        manager._health_monitor._cached_daemon_status = "ready"
        cast(Any, manager._health_monitor).get_cached_status = MagicMock(
            return_value=(True, None, "running", None)
        )

        # Mock _session_manager.get to return None for get() to avoid pre-created session path
        if manager._event_handlers._session_manager:
            cast(Any, manager._event_handlers._session_manager).get = MagicMock(return_value=None)

        # Replace _session_manager and _session_task_manager with mocks
        # so tests can set return_value on their methods
        manager._session_manager = MagicMock()
        manager._session_task_manager = MagicMock()
        # Update session lookup service references to use the mocked instances
        manager._session_lookup._session_manager = manager._session_manager
        manager._session_lookup._session_task_manager = manager._session_task_manager

        yield manager

        # Cleanup
        manager.shutdown()


def test_hook_event_task_id(mock_hook_manager: Any) -> None:
    """Test that task_id is populated in HookEvent during handling."""

    # Setup
    external_id = "test-session-123"
    platform_session_id = "session-uuid"
    task_id = "task-123"
    task_title = "Test Task"

    # Mock session lookup
    mock_hook_manager._session_manager.get_session_id.return_value = platform_session_id

    # Mock active task lookup
    mock_task = MagicMock(spec=Task)
    mock_task.id = task_id
    mock_task.title = task_title
    mock_task.status = "in_progress"

    mock_hook_manager._session_task_manager.get_session_tasks.return_value = [
        {"task": mock_task, "action": "worked_on"}
    ]

    # Create event
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": "Hello"},
    )

    # Execute handler
    # We need to mock the specific handler to avoid side effects
    mock_handler = MagicMock(return_value=HookResponse(decision="allow"))
    with patch.object(mock_hook_manager._event_handlers, "get_handler", return_value=mock_handler):
        mock_hook_manager.handle(event)

    # Verify task_id was populated on the event object
    assert event.task_id == task_id
    assert event.metadata["_task_id_origin"] == "session_context"
    assert event.metadata["_task_title"] == task_title
    assert event.metadata["_platform_session_id"] == platform_session_id


def test_session_start_context_injection(mock_hook_manager: Any) -> None:
    """Task context rides the deferred startup packet staged on first activity."""

    external_id = "test-session-123"
    platform_session_id = "session-uuid"
    task_id = "task-123"
    task_title = "Important Feature"

    mock_task = MagicMock(spec=Task)
    mock_task.id = task_id
    mock_task.title = task_title
    mock_task.status = "in_progress"
    mock_hook_manager._session_task_manager.get_session_tasks.return_value = [
        {"task": mock_task, "action": "worked_on"}
    ]

    start_event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"cwd": "/tmp"},
        task_id=task_id,
        metadata={"_task_title": task_title},
    )
    with patch.object(
        mock_hook_manager._project_id_resolver,
        "resolve",
        return_value="test-project-id",
    ):
        response = mock_hook_manager._event_handlers.handle_session_start(start_event)

    # A cold startup defers row creation, so nothing is injected yet.
    assert response.decision == "allow"
    assert response.context is None
    assert "_platform_session_id" not in start_event.metadata

    session_obj = MagicMock()
    session_obj.project_id = "test-project-id"
    session_obj.parent_session_id = None
    session_obj.transcript_path = None
    session_obj.status = "active"
    mock_hook_manager._session_manager.get.return_value = session_obj
    activity = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": "Hello", "cwd": "/tmp"},
        project_id="test-project-id",
        task_id=task_id,
        metadata={"_task_title": task_title, "_platform_session_id": platform_session_id},
    )
    handlers = mock_hook_manager._event_handlers

    with (
        patch.object(mock_hook_manager, "_evaluate_workflow_rules", return_value=(None, None)),
        patch.object(mock_hook_manager, "_evaluate_blocking_webhooks", return_value=None),
        patch.object(
            handlers,
            "_compose_session_response",
            wraps=handlers._compose_session_response,
        ) as compose,
    ):
        assert (
            activate_deferred_session(
                mock_hook_manager,
                activity,
                BlockingEffectDeadline(123.0),
            )
            is None
        )

    assert compose.call_args.kwargs["task_id"] == task_id
    context = activity.metadata["_startup_context"]
    assert context is not None
    assert f"You are working on task: {task_title}" in context
    assert f"({task_id})" in context
