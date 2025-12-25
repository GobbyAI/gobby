"""Tests for the HookManager coordinator."""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.projects import LocalProjectManager


@pytest.fixture
def mock_daemon_client():
    """Create a mock daemon client."""
    client = MagicMock()
    # Mock check_status to return (is_ready, message, status, error)
    client.check_status.return_value = (True, "Daemon ready", "ready", None)
    return client


@pytest.fixture
def hook_manager_with_mocks(temp_dir: Path, mock_daemon_client: MagicMock):
    """Create a HookManager with mocked dependencies."""
    # Create temp database
    db_path = temp_dir / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)

    # Create a test project
    project_mgr = LocalProjectManager(db)
    project = project_mgr.create(name="test-project", repo_path=str(temp_dir))

    # Create project.json for auto-discovery
    gobby_dir = temp_dir / ".gobby"
    gobby_dir.mkdir()
    (gobby_dir / "project.json").write_text(f'{{"id": "{project.id}", "name": "test-project"}}')

    with patch("gobby.hooks.hook_manager.DaemonClient") as MockDaemonClient:
        MockDaemonClient.return_value = mock_daemon_client

        manager = HookManager(
            daemon_host="localhost",
            daemon_port=8765,
            log_file=str(temp_dir / "logs" / "hook-manager.log"),
        )

        # Pre-warm the daemon status cache
        manager._cached_daemon_is_ready = True
        manager._cached_daemon_status = "ready"

        yield manager

        # Cleanup
        manager.shutdown()
        db.close()


@pytest.fixture
def sample_session_start_event(temp_dir: Path) -> HookEvent:
    """Create a sample session start event."""
    return HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-external-id-123",
        source=SessionSource.CLAUDE,
        timestamp=datetime.utcnow(),
        data={
            "source": "startup",
            "cwd": str(temp_dir),
            "transcript_path": str(temp_dir / "transcript.jsonl"),
        },
        machine_id="test-machine-id",
    )


class TestHookManagerInit:
    """Tests for HookManager initialization."""

    def test_init_creates_subsystems(self, hook_manager_with_mocks: HookManager):
        """Test that initialization creates all subsystems."""
        manager = hook_manager_with_mocks

        assert manager._daemon_client is not None
        assert manager._transcript_processor is not None
        assert manager._session_manager is not None
        assert manager._summary_file_generator is not None
        assert manager._database is not None

    def test_init_sets_daemon_url(self, hook_manager_with_mocks: HookManager):
        """Test that daemon URL is set correctly."""
        manager = hook_manager_with_mocks
        assert manager.daemon_url == "http://localhost:8765"

    def test_init_creates_event_handler_map(self, hook_manager_with_mocks: HookManager):
        """Test that event handler map is created."""
        manager = hook_manager_with_mocks

        # Check key event types have handlers
        assert HookEventType.SESSION_START in manager._event_handler_map
        assert HookEventType.SESSION_END in manager._event_handler_map
        assert HookEventType.BEFORE_AGENT in manager._event_handler_map
        assert HookEventType.AFTER_AGENT in manager._event_handler_map
        assert HookEventType.BEFORE_TOOL in manager._event_handler_map
        assert HookEventType.AFTER_TOOL in manager._event_handler_map


class TestHookManagerHandle:
    """Tests for the handle() method."""

    def test_handle_returns_hook_response(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ):
        """Test that handle returns a HookResponse."""
        response = hook_manager_with_mocks.handle(sample_session_start_event)

        assert isinstance(response, HookResponse)
        assert response.decision == "allow"

    def test_handle_daemon_not_ready(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ):
        """Test handling when daemon is not ready."""
        manager = hook_manager_with_mocks

        # Simulate daemon not ready
        manager._cached_daemon_is_ready = False
        manager._cached_daemon_status = "not_running"
        manager._cached_daemon_error = "Connection refused"

        response = manager.handle(sample_session_start_event)

        # Should fail open
        assert response.decision == "allow"
        assert response.reason is not None
        assert "not_running" in response.reason

    def test_handle_unknown_event_type(self, hook_manager_with_mocks: HookManager):
        """Test handling unknown event type fails open."""
        manager = hook_manager_with_mocks

        # Remove a handler to simulate unknown event
        del manager._event_handler_map[HookEventType.NOTIFICATION]

        event = HookEvent(
            event_type=HookEventType.NOTIFICATION,
            session_id="test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.utcnow(),
            data={},
        )

        response = manager.handle(event)

        # Should fail open
        assert response.decision == "allow"


class TestHookManagerSessionStart:
    """Tests for session start handling."""

    def test_session_start_registers_session(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ):
        """Test that session start registers a new session."""
        response = hook_manager_with_mocks.handle(sample_session_start_event)

        assert response.decision == "allow"
        assert response.metadata.get("session_id") is not None
        assert response.metadata.get("external_id") == "test-external-id-123"

    def test_session_start_returns_context(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ):
        """Test that session start returns context."""
        response = hook_manager_with_mocks.handle(sample_session_start_event)

        assert response.context is not None
        assert "Session registered" in response.context


class TestHookManagerSessionEnd:
    """Tests for session end handling."""

    def test_session_end_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
        temp_dir: Path,
    ):
        """Test that session end is allowed."""
        # First start a session
        hook_manager_with_mocks.handle(sample_session_start_event)

        # Create transcript file in temp directory
        transcript_path = temp_dir / "transcript.jsonl"
        transcript_path.touch()

        # Then end it
        end_event = HookEvent(
            event_type=HookEventType.SESSION_END,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.utcnow(),
            data={"transcript_path": str(transcript_path)},
            machine_id="test-machine-id",
        )

        response = hook_manager_with_mocks.handle(end_event)
        assert response.decision == "allow"


class TestHookManagerBeforeAgent:
    """Tests for before agent (user prompt submit) handling."""

    def test_before_agent_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ):
        """Test that before agent is allowed."""
        # Start session first
        hook_manager_with_mocks.handle(sample_session_start_event)

        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.utcnow(),
            data={"prompt": "Help me write a function"},
            machine_id="test-machine-id",
        )

        response = hook_manager_with_mocks.handle(event)
        assert response.decision == "allow"


class TestHookManagerToolEvents:
    """Tests for tool event handling."""

    def test_before_tool_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ):
        """Test that before tool use is allowed."""
        hook_manager_with_mocks.handle(sample_session_start_event)

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.utcnow(),
            data={"tool_name": "bash", "tool_input": {"command": "ls"}},
            machine_id="test-machine-id",
        )

        response = hook_manager_with_mocks.handle(event)
        assert response.decision == "allow"

    def test_after_tool_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ):
        """Test that after tool use is allowed."""
        hook_manager_with_mocks.handle(sample_session_start_event)

        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.utcnow(),
            data={"tool_name": "bash", "tool_output": "file1.txt\nfile2.txt"},
            machine_id="test-machine-id",
        )

        response = hook_manager_with_mocks.handle(event)
        assert response.decision == "allow"


class TestHookManagerShutdown:
    """Tests for HookManager shutdown."""

    def test_shutdown_stops_health_check(self, hook_manager_with_mocks: HookManager):
        """Test that shutdown stops health check monitoring."""
        manager = hook_manager_with_mocks

        # Should have a timer running
        assert manager._health_check_timer is not None or manager._is_shutdown

        manager.shutdown()

        # Should be marked as shutdown
        assert manager._is_shutdown is True


class TestHookManagerGetEventHandler:
    """Tests for event handler lookup."""

    def test_get_handler_for_known_event(self, hook_manager_with_mocks: HookManager):
        """Test getting handler for known event type."""
        handler = hook_manager_with_mocks._get_event_handler(HookEventType.SESSION_START)
        assert handler is not None
        assert callable(handler)

    def test_get_handler_for_all_event_types(self, hook_manager_with_mocks: HookManager):
        """Test that all event types in map have handlers."""
        for event_type in hook_manager_with_mocks._event_handler_map:
            handler = hook_manager_with_mocks._get_event_handler(event_type)
            assert handler is not None


class TestHookManagerMachineId:
    """Tests for machine ID functionality."""

    def test_get_machine_id(self, hook_manager_with_mocks: HookManager):
        """Test getting machine ID returns a string."""
        result = hook_manager_with_mocks.get_machine_id()
        # Should return a string (either from cache, config, or generated)
        assert result is None or isinstance(result, str)


class TestHookManagerCachedDaemonStatus:
    """Tests for cached daemon status."""

    def test_get_cached_daemon_status(self, hook_manager_with_mocks: HookManager):
        """Test getting cached daemon status."""
        manager = hook_manager_with_mocks

        # Set cached values
        manager._cached_daemon_is_ready = True
        manager._cached_daemon_message = "Ready"
        manager._cached_daemon_status = "healthy"
        manager._cached_daemon_error = None

        is_ready, message, status, error = manager._get_cached_daemon_status()

        assert is_ready is True
        assert message == "Ready"
        assert status == "healthy"
        assert error is None
