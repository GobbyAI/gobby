"""Tests for the HookManager coordinator."""

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.utils.session_context import reset_seeded_contexts, resolve_and_seed_contexts
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_daemon_client():
    """Create a mock daemon client."""
    client = MagicMock()
    # Mock check_status to return (is_ready, message, status, error)
    client.check_status.return_value = (True, "Daemon ready", "ready", None)
    return client


@pytest.fixture
def hook_manager_with_mocks(
    temp_dir: Path,
    mock_daemon_client: MagicMock,
    hub_db: HubDatabase,
) -> HookManager:
    """Create a HookManager with mocked dependencies."""
    db = hub_db

    # Create a test project
    project_mgr = LocalProjectManager(db)
    project = project_mgr.create(name="test-project", repo_path=str(temp_dir))

    # Create project.json for auto-discovery
    gobby_dir = temp_dir / ".gobby"
    gobby_dir.mkdir()
    (gobby_dir / "project.json").write_text(f'{{"id": "{project.id}", "name": "test-project"}}')

    from gobby.config.app import DaemonConfig
    from gobby.config.extensions import HookExtensionsConfig, WebhooksConfig

    # Create config with disabled webhooks.
    test_config = DaemonConfig(
        hook_extensions=HookExtensionsConfig(
            webhooks=WebhooksConfig(enabled=False),
        ),
    )

    with (
        patch("gobby.hooks.factory.DaemonClient") as MockDaemonClient,
        patch("gobby.hooks.webhooks.httpx.AsyncClient") as MockHttpClient,
    ):
        MockDaemonClient.return_value = mock_daemon_client
        MockHttpClient.return_value = MagicMock()

        manager = HookManager(
            daemon_host="localhost",
            daemon_port=60887,
            config=test_config,
            database=db,
            log_file=str(temp_dir / "logs" / "hook-manager.log"),
        )

        # Pre-warm the daemon status cache
        manager._health_monitor._cached_daemon_is_ready = True
        manager._health_monitor._cached_daemon_status = "ready"

        yield manager

        # Cleanup: the database itself is owned by the hub_db fixture.
        manager.shutdown()


@pytest.fixture
def sample_session_start_event(temp_dir: Path) -> HookEvent:
    """Create a sample session start event."""
    return HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-external-id-123",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "source": "startup",
            "cwd": str(temp_dir),
            "transcript_path": str(temp_dir / "transcript.jsonl"),
        },
        machine_id="test-machine-id",
    )


class TestHookManagerInit:
    """Tests for HookManager initialization."""

    def test_init_creates_subsystems(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that initialization creates all subsystems."""
        manager = hook_manager_with_mocks

        assert manager._daemon_client is not None
        assert manager._transcript_processor is not None
        assert manager._session_manager is not None
        assert manager._database is not None

    def test_init_has_skill_manager(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that HookManager has a skill_manager."""
        manager = hook_manager_with_mocks

        # Verify skill_manager exists on HookManager
        assert hasattr(manager, "_skill_manager")
        assert manager._skill_manager is not None

    def test_init_sets_daemon_url(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that daemon URL is set correctly."""
        manager = hook_manager_with_mocks
        assert manager.daemon_url == "http://localhost:60887"

    def test_init_creates_event_handlers(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that event handlers are created."""
        manager = hook_manager_with_mocks
        handler_map = manager._event_handlers.get_handler_map()

        # Check key event types have handlers
        assert HookEventType.SESSION_START in handler_map
        assert HookEventType.SESSION_END in handler_map
        assert HookEventType.BEFORE_AGENT in handler_map
        assert HookEventType.AFTER_AGENT in handler_map
        assert HookEventType.BEFORE_TOOL in handler_map
        assert HookEventType.AFTER_TOOL in handler_map


class TestHookManagerHandle:
    """Tests for the handle() method."""

    def test_handle_returns_hook_response(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test that handle returns a HookResponse."""
        response = hook_manager_with_mocks.handle(sample_session_start_event)

        assert isinstance(response, HookResponse)
        assert response.decision == "allow"

    def test_handle_daemon_not_ready(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test handling when daemon is not ready."""
        from unittest.mock import patch

        manager = hook_manager_with_mocks

        # Simulate daemon not ready by mocking HealthMonitor's get_cached_status
        # Also mock check_now() since critical hooks (like SESSION_START) retry
        with (
            patch.object(
                manager._health_monitor,
                "get_cached_status",
                return_value=(False, None, "not_running", "Connection refused"),
            ),
            patch.object(
                manager._health_monitor,
                "check_now",
                return_value=False,  # Retries also fail
            ),
        ):
            response = manager.handle(sample_session_start_event)

        # Should fail open
        assert response.decision == "allow"
        assert response.reason is not None
        assert "not_running" in response.reason

    def test_handle_daemon_recovers_after_retry(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test daemon recovery after retry for critical hooks."""
        from unittest.mock import MagicMock, patch

        manager = hook_manager_with_mocks

        # Create a mock that returns True on the second call (simulating recovery)
        check_now_mock = MagicMock(side_effect=[False, True])  # Fails first, then succeeds

        # Simulate daemon not ready initially, but check_now recovers
        with (
            patch.object(
                manager._health_monitor,
                "get_cached_status",
                return_value=(False, None, "not_running", "Connection refused"),
            ),
            patch.object(
                manager._health_monitor,
                "check_now",
                check_now_mock,
            ),
        ):
            response = manager.handle(sample_session_start_event)

        # Should succeed after retry (no reason means it proceeded to handler)
        assert response.decision == "allow"
        # check_now should have been called (retry logic was triggered)
        assert check_now_mock.call_count >= 1

    def test_handle_unknown_event_type(self, hook_manager_with_mocks: HookManager) -> None:
        """Test handling unknown event type fails open."""
        from unittest.mock import patch

        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.NOTIFICATION,
            session_id="test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
        )

        # Mock the event handlers to return None for any event type
        with patch.object(manager._event_handlers, "get_handler", return_value=None):
            response = manager.handle(event)

        # Should fail open
        assert response.decision == "allow"

    def test_non_session_end_hook_revives_expired_terminal_session(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        """Hook activity repairs false-expired terminal sessions before handling."""
        manager = hook_manager_with_mocks
        project_id = manager._resolve_project_id(None, str(temp_dir))
        session = manager._session_manager.register(
            external_id="codex-ext-1",
            machine_id="test-machine-id",
            source="codex",
            project_id=project_id,
            transcript_path=str(temp_dir / "codex.jsonl"),
            terminal_context={"parent_pid": 99999},
        )
        manager._session_manager.update_status(session.id, "expired")

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="codex-ext-1",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir), "tool_name": "shell", "tool_input": {}},
            machine_id="test-machine-id",
            cwd=str(temp_dir),
        )

        response = manager.handle(event)

        assert response.decision == "allow"
        revived = manager._session_manager.get(session.id)
        assert revived is not None
        assert revived.status == "active"


class TestHookManagerSessionStart:
    """Tests for session start handling."""

    def test_session_start_registers_session(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test that session start registers a new session."""
        response = hook_manager_with_mocks.handle(sample_session_start_event)

        assert response.decision == "allow"
        assert response.metadata.get("session_id") is not None
        assert response.metadata.get("external_id") == "test-external-id-123"

    def test_session_start_returns_response(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test that session start returns a valid response with system_message."""
        response = hook_manager_with_mocks.handle(sample_session_start_event)

        assert response.decision == "allow"
        # Response should include session ID banner in system_message
        assert response.system_message is not None
        assert "Gobby Session ID:" in response.system_message
        # External ID now in metadata (injected by enricher), not system_message
        assert response.metadata.get("external_id") is not None

    def test_session_resume_no_handoff_message(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        """Test that resume source doesn't show 'Context restored' system_message.

        Parent session finding only happens on source='clear' (handoff scenario).
        On resume we get basic session info only, no parent context.
        """
        # Create a resume event (source="resume" means continuing same session)
        resume_event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="test-resume-session-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "source": "resume",  # Key: this is a resume, not startup
                "cwd": str(temp_dir),
                "transcript_path": str(temp_dir / "transcript.jsonl"),
            },
            machine_id="test-machine-id",
        )

        response = hook_manager_with_mocks.handle(resume_event)

        # Should be allowed
        assert response.decision == "allow"

        # Should have session ID banner but NOT "Context restored" message
        # Parent finding only runs on source='clear'
        assert response.system_message is not None
        assert "Gobby Session ID:" in response.system_message
        assert "Context restored" not in (response.system_message or "")


class TestHookManagerSessionEnd:
    """Tests for session end handling."""

    def test_session_end_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
        temp_dir: Path,
    ) -> None:
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
            timestamp=datetime.now(UTC),
            data={"transcript_path": str(transcript_path)},
            machine_id="test-machine-id",
        )

        response = hook_manager_with_mocks.handle(end_event)
        assert response.decision == "allow"

    @pytest.mark.integration
    def test_session_end_auto_links_commits(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        """Test that session end auto-links commits made during session."""
        from gobby.storage.session_models import Session
        from gobby.tasks.commits import AutoLinkResult

        # Create transcript file in temp directory
        transcript_path = temp_dir / "transcript.jsonl"
        transcript_path.touch()

        # Mock session storage to return a session with created_at
        mock_session = Session(
            id="test-session-id",
            external_id="test-external-id-123",
            machine_id="test-machine-id",
            source="claude",
            project_id="test-project-id",
            title="Test Session",
            status="active",
            transcript_path=None,
            summary_path=None,
            summary_markdown=None,
            git_branch=None,
            parent_session_id=None,
            created_at="2026-01-04T00:00:00+00:00",
            updated_at="2026-01-04T00:00:00+00:00",
        )

        # Mock auto_link_commits to verify it's called
        mock_result = AutoLinkResult(
            linked_tasks={"gt-123abc": ["abc1234", "def5678"]},
            total_linked=2,
            skipped=1,
        )

        with (
            patch.object(
                hook_manager_with_mocks._session_manager,
                "get_session_id",
                return_value="test-session-id",
            ),
            patch.object(
                hook_manager_with_mocks._session_manager, "get", return_value=mock_session
            ),
            patch(
                "gobby.tasks.commits.auto_link_commits", return_value=mock_result
            ) as mock_auto_link,
        ):
            end_event = HookEvent(
                event_type=HookEventType.SESSION_END,
                session_id="test-external-id-123",
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                data={"transcript_path": str(transcript_path), "cwd": str(temp_dir)},
                machine_id="test-machine-id",
                metadata={"_platform_session_id": "test-session-id"},
            )

            response = hook_manager_with_mocks.handle(end_event)

            assert response.decision == "allow"

            # Verify auto_link_commits was called
            mock_auto_link.assert_called_once()
            call_kwargs = mock_auto_link.call_args.kwargs
            assert "task_manager" in call_kwargs
            assert call_kwargs["since"] == "2026-01-04T00:00:00+00:00"
            assert call_kwargs["cwd"] == str(temp_dir)


class TestHookManagerBeforeAgent:
    """Tests for before agent (user prompt submit) handling."""

    def test_before_agent_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test that before agent is allowed."""
        # Start session first
        hook_manager_with_mocks.handle(sample_session_start_event)

        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
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
    ) -> None:
        """Test that before tool use is allowed."""
        hook_manager_with_mocks.handle(sample_session_start_event)

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash", "tool_input": {"command": "ls"}},
            machine_id="test-machine-id",
        )

        response = hook_manager_with_mocks.handle(event)
        assert response.decision == "allow"

    def test_after_tool_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test that after tool use is allowed."""
        hook_manager_with_mocks.handle(sample_session_start_event)

        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash", "tool_output": "file1.txt\nfile2.txt"},
            machine_id="test-machine-id",
        )

        response = hook_manager_with_mocks.handle(event)
        assert response.decision == "allow"


class TestHookManagerShutdown:
    """Tests for HookManager shutdown."""

    def test_shutdown_stops_health_check(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that shutdown stops health check monitoring."""
        manager = hook_manager_with_mocks

        # Should have a health monitor with timer running or already shutdown
        assert (
            manager._health_monitor._health_check_timer is not None
            or manager._health_monitor._is_shutdown
        )

        manager.shutdown()

        # Should be marked as shutdown in the health monitor
        assert manager._health_monitor._is_shutdown is True


class TestHookManagerGetEventHandler:
    """Tests for event handler lookup."""

    def test_get_handler_for_known_event(self, hook_manager_with_mocks: HookManager) -> None:
        """Test getting handler for known event type."""
        handler = hook_manager_with_mocks._get_event_handler(HookEventType.SESSION_START)
        assert handler is not None
        assert callable(handler)

    def test_get_handler_for_all_event_types(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that all event types in map have handlers."""
        handler_map = hook_manager_with_mocks._event_handlers.get_handler_map()
        for event_type in handler_map:
            handler = hook_manager_with_mocks._get_event_handler(event_type)
            assert handler is not None


class TestHookManagerMachineId:
    """Tests for machine ID functionality."""

    def test_get_machine_id(self, hook_manager_with_mocks: HookManager) -> None:
        """Test getting machine ID returns a string."""
        result = hook_manager_with_mocks.get_machine_id()
        # Should return a string (either from cache, config, or generated)
        assert result is None or isinstance(result, str)


class TestHookManagerCachedDaemonStatus:
    """Tests for cached daemon status."""

    def test_get_cached_daemon_status(self, hook_manager_with_mocks: HookManager) -> None:
        """Test getting cached daemon status."""
        manager = hook_manager_with_mocks

        # Set cached values on the health monitor (delegation target)
        manager._health_monitor._cached_daemon_is_ready = True
        manager._health_monitor._cached_daemon_message = "Ready"
        manager._health_monitor._cached_daemon_status = "healthy"
        manager._health_monitor._cached_daemon_error = None

        is_ready, message, status, error = manager._get_cached_daemon_status()

        assert is_ready is True
        assert message == "Ready"
        assert status == "healthy"
        assert error is None


class TestHookManagerConfigLoadError:
    """Tests for config loading error handling."""

    def test_init_handles_config_load_error(
        self,
        temp_dir: Path,
        mock_daemon_client: MagicMock,
        hub_db: HubDatabase,
    ) -> None:
        """Test that init handles config loading errors gracefully."""
        with (
            patch("gobby.hooks.factory.DaemonClient") as MockDaemonClient,
            patch("gobby.config.app.load_config", side_effect=Exception("Config load failed")),
        ):
            MockDaemonClient.return_value = mock_daemon_client

            # Should not raise - handles error gracefully
            manager = HookManager(
                daemon_host="localhost",
                daemon_port=60887,
                config=None,  # Force config loading
                database=hub_db,
                log_file=str(temp_dir / "logs" / "hook-manager.log"),
            )

            # Manager should still be created with defaults
            assert manager is not None
            assert manager._config is None  # Config was not loaded

            manager.shutdown()

    def test_init_uses_default_health_check_interval_without_config(
        self,
        temp_dir: Path,
        mock_daemon_client: MagicMock,
        hub_db: HubDatabase,
    ) -> None:
        """Test that init uses default health check interval when config is None."""
        with (
            patch("gobby.hooks.factory.DaemonClient") as MockDaemonClient,
            patch("gobby.config.app.load_config", side_effect=Exception("Config load failed")),
        ):
            MockDaemonClient.return_value = mock_daemon_client

            manager = HookManager(
                daemon_host="localhost",
                daemon_port=60887,
                config=None,
                database=hub_db,
                log_file=str(temp_dir / "logs" / "hook-manager.log"),
            )

            # Health check should still work with defaults
            assert manager._health_monitor is not None

            manager.shutdown()


class TestHookManagerWorkflowBlocking:
    """Tests for workflow blocking behavior."""

    def test_handle_workflow_blocks_event(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that workflow can block an event."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-workflow-block",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="test-machine-id",
        )

        # Mock workflow handler to return block decision
        with patch.object(
            manager._workflow_handler,
            "handle",
            return_value=HookResponse(decision="block", reason="Workflow blocked"),
        ):
            response = manager.handle(event)

        assert response.decision == "block"
        assert response.reason == "Workflow blocked"

    def test_handle_workflow_ask_decision(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that workflow can return ask decision."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-workflow-ask",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="test-machine-id",
        )

        # Mock workflow handler to return ask decision
        with patch.object(
            manager._workflow_handler,
            "handle",
            return_value=HookResponse(decision="ask", reason="Need confirmation"),
        ):
            response = manager.handle(event)

        assert response.decision == "ask"
        assert response.reason == "Need confirmation"

    def test_handle_workflow_context_merged(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that workflow context is merged into response."""
        manager = hook_manager_with_mocks

        # Mock workflow handler to return context
        workflow_response = HookResponse(decision="allow", context="Workflow context info")
        with patch.object(manager._workflow_handler, "handle", return_value=workflow_response):
            response = manager.handle(sample_session_start_event)

        assert response.decision == "allow"
        assert "Workflow context info" in (response.context or "")

    def test_handle_workflow_error_fails_open(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that workflow errors fail open."""
        manager = hook_manager_with_mocks

        # Mock workflow handler to raise exception
        with patch.object(
            manager._workflow_handler,
            "handle",
            side_effect=Exception("Workflow engine error"),
        ):
            response = manager.handle(sample_session_start_event)

        # Should still allow (fail-open)
        assert response.decision == "allow"


class TestHookManagerWebhookBlocking:
    """Tests for webhook blocking behavior."""

    def test_handle_webhook_blocks_event(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that blocking webhook can block an event."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-webhook-block",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="test-machine-id",
        )

        # Mock webhook dispatcher to return block decision
        with (
            patch.object(manager, "_dispatch_webhooks_sync", return_value=[MagicMock()]),
            patch.object(
                manager._webhook_dispatcher,
                "get_blocking_decision",
                return_value=("block", "Webhook rejected"),
            ),
        ):
            response = manager.handle(event)

        assert response.decision == "block"
        assert "Webhook rejected" in response.reason

    def test_handle_webhook_error_fails_open(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that webhook errors fail open."""
        manager = hook_manager_with_mocks

        # Mock webhook dispatch to raise exception
        with patch.object(
            manager, "_dispatch_webhooks_sync", side_effect=Exception("Webhook error")
        ):
            response = manager.handle(sample_session_start_event)

        # Should still allow (fail-open)
        assert response.decision == "allow"


class TestHookManagerHandlerErrors:
    """Tests for handler error handling."""

    def test_handle_handler_exception_fails_open(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that handler exceptions fail open."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="test-handler-error",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir)},
            machine_id="test-machine-id",
        )

        # Mock handler to raise exception
        def failing_handler(evt):
            raise Exception("Handler crashed")

        with patch.object(manager._event_handlers, "get_handler", return_value=failing_handler):
            response = manager.handle(event)

        assert response.decision == "allow"
        assert "Handler error:" in response.reason


class TestHookManagerBroadcasting:
    """Tests for event broadcasting."""

    def test_handle_broadcasts_event_with_loop(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that events are broadcast when broadcaster is configured."""
        import asyncio

        manager = hook_manager_with_mocks

        mock_broadcaster = MagicMock()

        async def mock_broadcast(*args, **kwargs):
            return None

        mock_broadcaster.broadcast_event = MagicMock(side_effect=mock_broadcast)
        manager.broadcaster = mock_broadcaster

        # Simulate running in an event loop
        async def run_in_loop():
            return manager.handle(sample_session_start_event)

        asyncio.run(run_in_loop())

        # Broadcaster should have been called
        assert mock_broadcaster.broadcast_event.called

    def test_handle_broadcasts_event_threadsafe(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that events are broadcast thread-safely when no loop is running."""
        import asyncio
        import threading

        manager = hook_manager_with_mocks

        mock_broadcaster = MagicMock()
        broadcasted = threading.Event()

        async def mock_broadcast(*args, **kwargs):
            broadcasted.set()
            return None

        mock_broadcaster.broadcast_event = MagicMock(side_effect=mock_broadcast)
        manager.broadcaster = mock_broadcaster

        # Create a loop for thread-safe scheduling and run it in a thread
        loop = asyncio.new_event_loop()
        manager._loop = loop

        import threading

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        try:
            # Call handle outside of event loop
            manager.handle(sample_session_start_event)
            assert broadcasted.wait(timeout=1)
        finally:
            manager._loop = None
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=1)
            loop.close()
        assert mock_broadcaster.broadcast_event.called

    def test_handle_no_loop_no_broadcaster_error(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that handle works without event loop and no broadcaster."""
        manager = hook_manager_with_mocks
        manager.broadcaster = MagicMock()
        manager._loop = None

        # Should not raise
        response = manager.handle(sample_session_start_event)
        assert response.decision == "allow"

    def test_handle_broadcast_threadsafe_error(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that broadcast errors from run_coroutine_threadsafe are handled."""
        import asyncio
        import warnings

        manager = hook_manager_with_mocks

        mock_broadcaster = MagicMock()

        async def mock_broadcast(*args, **kwargs):
            return None

        mock_broadcaster.broadcast_event = MagicMock(side_effect=mock_broadcast)
        manager.broadcaster = mock_broadcaster

        # Create a closed loop to trigger error
        loop = asyncio.new_event_loop()
        loop.close()
        manager._loop = loop

        # Suppress the "coroutine was never awaited" warning since we're testing error handling
        # with a closed loop that can't run the coroutine
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="coroutine .* was never awaited")
            # Should not raise - error is logged
            response = manager.handle(sample_session_start_event)
        assert response.decision == "allow"

    def test_handle_dispatch_webhooks_async_error(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that async webhook dispatch errors are handled."""
        manager = hook_manager_with_mocks

        # Mock _dispatch_webhooks_async to raise exception
        with patch.object(
            manager, "_dispatch_webhooks_async", side_effect=Exception("Webhook error")
        ):
            # Should not raise - error is logged
            response = manager.handle(sample_session_start_event)

        assert response.decision == "allow"


class TestHookManagerSessionLookup:
    """Tests for session lookup and auto-registration."""

    def test_session_start_precreated_session_skips_auto_registration(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Pre-created SESSION_START rows must bind in-place without a stray auto-register."""
        manager = hook_manager_with_mocks
        project_meta = (temp_dir / ".gobby" / "project.json").read_text()
        project_id = json.loads(project_meta)["id"]
        precreated = manager._session_manager.create_web_chat_session(
            machine_id="test-machine-id",
            project_id=project_id,
            source="codex",
            model="gpt-5.4",
            sandbox_enabled=False,
            sandbox_policy_hash="policy-hash",
        )

        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=precreated.id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir), "source": "startup"},
            machine_id="test-machine-id",
        )

        with patch.object(
            manager._session_manager,
            "register_session",
            wraps=manager._session_manager.register_session,
        ) as mock_register:
            response = manager.handle(event)

        assert response.decision == "allow"
        mock_register.assert_not_called()
        assert event.metadata["_platform_session_id"] == precreated.id

    def test_resumed_codex_ignores_stale_wrapper_metadata_for_session_context(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A stale injected wrapper id must be replaced by the canonical Codex session."""
        manager = hook_manager_with_mocks
        external_id = "resumed-codex-session"

        start_event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=external_id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "cwd": str(temp_dir),
                "source": "startup",
                "transcript_path": str(temp_dir / "resumed-codex.jsonl"),
            },
            machine_id="test-machine-id",
        )
        response = manager.handle(start_event)

        assert response.decision == "allow"
        canonical_id = start_event.metadata["_platform_session_id"]
        stale_wrapper_id = str(uuid.uuid4())
        assert stale_wrapper_id != canonical_id

        caplog.set_level(logging.WARNING)
        caplog.clear()
        resumed_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=external_id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {
                    "name": "wrapper_recovery",
                    "value": True,
                    "session_id": canonical_id,
                },
                "cwd": str(temp_dir),
            },
            machine_id="test-machine-id",
            metadata={"_platform_session_id": stale_wrapper_id},
        )
        response = manager.handle(resumed_event)

        assert response.decision == "allow"
        assert resumed_event.metadata["_platform_session_id"] == canonical_id

        tokens = resolve_and_seed_contexts(
            session_ref=resumed_event.metadata["_platform_session_id"],
            session_manager=manager._session_manager,
            db=manager._database,
        )
        try:
            assert tokens.resolved_session_id == canonical_id
            variables = SessionVariableManager(manager._database)
            variables.set_variable(tokens.resolved_session_id, "wrapper_recovery", True)
            assert variables.get_variables(canonical_id)["wrapper_recovery"] is True
            assert variables.get_variables(stale_wrapper_id) == {}
            warning_messages = [record.getMessage() for record in caplog.records]
            assert not any("Session not found" in message for message in warning_messages)
            assert not any(
                "could not resolve session ref" in message for message in warning_messages
            )
        finally:
            reset_seeded_contexts(tokens)

    def test_handle_looks_up_session_from_database(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that session is looked up from database when not in cache."""
        manager = hook_manager_with_mocks

        # Create an event for a non-cached session
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="unknown-session-id",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash", "cwd": str(temp_dir)},
            machine_id="test-machine-id",
        )

        # Session not in cache, should query database
        with patch.object(manager._session_manager, "get_session_id", return_value=None):
            response = manager.handle(event)

        # Should still allow (session will be auto-registered)
        assert response.decision == "allow"

    def test_handle_auto_registers_unknown_session(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that unknown sessions are auto-registered."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="auto-register-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "bash",
                "cwd": str(temp_dir),
                "transcript_path": str(temp_dir / "transcript.jsonl"),
            },
            machine_id="test-machine-id",
        )

        # Session not in cache or database
        with (
            patch.object(manager._session_manager, "get_session_id", return_value=None),
            patch.object(manager._session_manager, "lookup_session_id", return_value=None),
            patch.object(
                manager._session_manager,
                "register_session",
                return_value="new-session-id",
            ) as mock_register,
        ):
            response = manager.handle(event)

        # Should have called register_session
        assert mock_register.called
        assert response.decision == "allow"

    def test_handle_does_not_auto_register_unknown_session_end(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Unknown SESSION_END hooks should not create placeholder session rows."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.SESSION_END,
            session_id="orphaned-session-end",
            source=SessionSource.QWEN,
            timestamp=datetime.now(UTC),
            data={
                "cwd": str(temp_dir),
                "transcript_path": str(temp_dir / "missing-transcript.jsonl"),
            },
            machine_id="test-machine-id",
        )

        with (
            patch.object(manager._session_manager, "get_session_id", return_value=None),
            patch.object(manager._session_manager, "lookup_session_id", return_value=None),
            patch.object(manager._session_manager, "recover_session", return_value=None),
        ):
            response = manager.handle(event)

        rows = manager._session_manager.db.fetchall(
            "SELECT id FROM sessions WHERE external_id = ?",
            ("orphaned-session-end",),
        )

        assert response.decision == "allow"
        assert event.metadata.get("_platform_session_id") is None
        assert rows == []

    def test_handle_recovers_existing_session_across_source_mismatch(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Later hooks with the wrong source should reuse the existing row."""
        manager = hook_manager_with_mocks
        project_meta = (temp_dir / ".gobby" / "project.json").read_text()
        project_id = json.loads(project_meta)["id"]
        existing = manager._session_manager.register(
            external_id="shared-session-id",
            machine_id="test-machine-id",
            source="codex",
            project_id=project_id,
            transcript_path=str(temp_dir / "rollout-shared-session-id.jsonl"),
            title="Recovered Session",
        )
        existing_session_id = existing.id

        wrong_source_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="shared-session-id",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash", "cwd": str(temp_dir)},
            machine_id="test-machine-id",
        )

        response = manager.handle(wrong_source_event)

        assert response.decision == "allow"
        assert (
            manager._session_manager.get_session_id("shared-session-id", "claude")
            == existing_session_id
        )
        rows = manager._session_manager.db.fetchall(
            "SELECT id FROM sessions WHERE external_id = ?",
            ("shared-session-id",),
        )
        assert len(rows) == 1

    def test_handle_backfills_terminal_context_for_known_session(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Later Codex hooks should repair a session that missed SessionStart terminal metadata."""
        manager = hook_manager_with_mocks

        start_event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="codex-missing-terminal-context",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir)},
            machine_id="test-machine-id",
        )
        manager.handle(start_event)

        session_id = manager._session_manager.get_session_id(
            "codex-missing-terminal-context",
            "codex",
        )
        assert session_id is not None

        manager._session_manager.db.execute(
            "UPDATE sessions SET title = ?, digest_markdown = ? WHERE id = ?",
            ("Recovered Codex Title", None, session_id),
        )

        repair_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="codex-missing-terminal-context",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "Bash",
                "cwd": str(temp_dir),
                "terminal_context": {"tmux_pane": "%5", "parent_pid": 999},
            },
            machine_id="test-machine-id",
        )

        with patch("gobby.hooks.session_lookup.schedule_tmux_window_rename") as mock_schedule:
            response = manager.handle(repair_event)

        assert response.decision == "allow"
        updated = manager._session_manager.get(session_id)
        assert updated is not None
        assert updated.terminal_context is not None
        assert updated.terminal_context["tmux_pane"] == "%5"
        mock_schedule.assert_called_once()

    def test_handle_resolves_active_task(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that active task is resolved for session."""
        manager = hook_manager_with_mocks

        # First register a session
        start_event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="task-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir)},
            machine_id="test-machine-id",
        )
        manager.handle(start_event)

        # Now trigger a tool event with mocked task
        tool_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="task-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="test-machine-id",
        )

        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_task.title = "Test Task"
        mock_task.status = "in_progress"

        with patch.object(
            manager._session_task_manager,
            "get_session_tasks",
            return_value=[{"action": "worked_on", "task": mock_task}],
        ):
            response = manager.handle(tool_event)

        assert response.decision == "allow"
        # Task context should be in event metadata
        assert tool_event.task_id == "gt-test123"

    def test_handle_task_resolution_error(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that task resolution errors are handled gracefully."""
        manager = hook_manager_with_mocks

        # First register a session
        start_event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="task-error-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir)},
            machine_id="test-machine-id",
        )
        manager.handle(start_event)

        tool_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="task-error-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="test-machine-id",
        )

        with patch.object(
            manager._session_task_manager,
            "get_session_tasks",
            side_effect=Exception("Database error"),
        ):
            response = manager.handle(tool_event)

        # Should still allow (error handled gracefully)
        assert response.decision == "allow"


class TestHookManagerWebhookDispatch:
    """Tests for webhook dispatch methods."""

    def test_dispatch_webhooks_sync_disabled(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that sync webhook dispatch returns empty when disabled."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="test-machine-id",
        )

        # Disable webhooks
        manager._webhook_dispatcher.config.enabled = False

        result = manager._dispatch_webhooks_sync(event)
        assert result == []

    def test_dispatch_webhooks_sync_no_matching_endpoints(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that sync webhook dispatch returns empty when no matching endpoints."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="test-machine-id",
        )

        # Enable webhooks but have no endpoints
        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = []

        result = manager._dispatch_webhooks_sync(event)
        assert result == []

    def test_dispatch_webhooks_sync_with_matching_endpoints(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that sync webhook dispatch works with matching endpoints."""
        from gobby.config.extensions import WebhookEndpointConfig

        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="test-machine-id",
        )

        # Create a blocking endpoint
        endpoint = WebhookEndpointConfig(
            name="test-webhook",
            url="https://example.com/webhook",
            events=["before_tool"],
            can_block=True,
            enabled=True,
        )

        # Enable webhooks with a blocking endpoint
        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = [endpoint]

        # Mock the dispatch to avoid actual HTTP calls
        from gobby.hooks.webhooks import WebhookResult

        mock_result = WebhookResult(
            endpoint_name="test-webhook",
            success=True,
            status_code=200,
            response_body={"action": "allow"},
        )

        with (
            patch.object(manager._webhook_dispatcher, "_build_payload", return_value={}),
            patch.object(
                manager._webhook_dispatcher,
                "_dispatch_single",
                return_value=mock_result,
            ),
        ):
            result = manager._dispatch_webhooks_sync(event, blocking_only=True)

        assert len(result) == 1
        assert result[0].success is True

    def test_dispatch_webhooks_async_disabled(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that async webhook dispatch does nothing when disabled."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-async-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="test-machine-id",
        )

        # Disable webhooks
        manager._webhook_dispatcher.config.enabled = False

        result = manager._dispatch_webhooks_async(event)
        assert result is None

    def test_dispatch_webhooks_async_no_matching_endpoints(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that async webhook dispatch does nothing when no matching endpoints."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-async-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="test-machine-id",
        )

        # Enable webhooks but have no non-blocking endpoints
        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = []

        result = manager._dispatch_webhooks_async(event)
        assert result is None

    def test_dispatch_webhooks_async_with_matching_endpoints(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that async webhook dispatch schedules tasks for matching endpoints."""
        import asyncio
        import threading

        from gobby.config.extensions import WebhookEndpointConfig

        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-async-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="test-machine-id",
        )

        # Create a non-blocking endpoint
        endpoint = WebhookEndpointConfig(
            name="test-async-webhook",
            url="https://example.com/webhook",
            events=["before_tool"],
            can_block=False,
            enabled=True,
        )

        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = [endpoint]

        # Create a loop for async dispatch
        loop = asyncio.new_event_loop()
        manager._loop = loop

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()
        dispatched = threading.Event()

        try:

            async def mock_dispatch(*args, **kwargs):
                dispatched.set()
                return None

            with (
                patch.object(manager._webhook_dispatcher, "_build_payload", return_value={}),
                patch.object(
                    manager._webhook_dispatcher,
                    "_dispatch_single",
                    new_callable=AsyncMock,
                    side_effect=mock_dispatch,
                ) as mock_dispatch_single,
            ):
                # Should schedule async task
                manager._dispatch_webhooks_async(event)
                assert dispatched.wait(timeout=1), "Async webhook dispatch never ran"
                assert mock_dispatch_single.await_count == 1

                async def no_op() -> None:
                    return None

                asyncio.run_coroutine_threadsafe(no_op(), loop).result(timeout=1)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=1)
            loop.close()

    def test_dispatch_webhooks_async_within_running_loop(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that async webhook dispatch creates task when inside running loop."""
        import asyncio

        from gobby.config.extensions import WebhookEndpointConfig

        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-async-loop-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="test-machine-id",
        )

        # Create a non-blocking endpoint
        endpoint = WebhookEndpointConfig(
            name="test-loop-webhook",
            url="https://example.com/webhook",
            events=["before_tool"],
            can_block=False,
            enabled=True,
        )

        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = [endpoint]

        async def run_dispatch():
            with (
                patch.object(manager._webhook_dispatcher, "_build_payload", return_value={}),
                patch.object(
                    manager._webhook_dispatcher,
                    "_dispatch_single",
                    new_callable=AsyncMock,
                ),
            ):
                manager._dispatch_webhooks_async(event)
                await wait_for_async_condition(
                    lambda: manager._webhook_dispatcher._dispatch_single.await_count == 1,
                    description="webhook dispatch",
                )
                assert manager._webhook_dispatcher._dispatch_single.await_count == 1

        asyncio.run(run_dispatch())


class TestHookManagerShutdownWebhook:
    """Tests for shutdown webhook cleanup."""

    def test_shutdown_closes_webhook_dispatcher_with_loop(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that shutdown closes webhook dispatcher when loop is available."""
        import asyncio

        manager = hook_manager_with_mocks

        # Set up a loop in a separate thread (like in real async context)
        import threading

        loop = asyncio.new_event_loop()
        manager._loop = loop

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        try:
            manager.shutdown()
        finally:
            manager._loop = None
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=1)
            loop.close()

        assert manager._health_monitor._is_shutdown is True

    def test_shutdown_closes_webhook_dispatcher_without_loop(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that shutdown closes webhook dispatcher when no loop is available."""
        manager = hook_manager_with_mocks
        manager._loop = None

        # Should not raise
        manager.shutdown()

        assert manager._health_monitor._is_shutdown is True

    def test_shutdown_handles_webhook_close_error(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that shutdown handles webhook dispatcher close errors."""
        manager = hook_manager_with_mocks

        # Mock close to raise exception
        async def failing_close():
            raise Exception("Close failed")

        manager._webhook_dispatcher.close = failing_close
        manager._loop = None

        # Should not raise - error is logged
        manager.shutdown()

        assert manager._health_monitor._is_shutdown is True


class TestHookManagerResolveProjectId:
    """Tests for project ID resolution."""

    def test_resolve_project_id_returns_provided_id(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that provided project ID is returned directly."""
        manager = hook_manager_with_mocks

        result = manager._resolve_project_id("my-project-id", "/some/path")
        assert result == "my-project-id"

    def test_resolve_project_id_from_project_context(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that project ID is resolved from project.json."""
        manager = hook_manager_with_mocks

        # Create project.json
        gobby_dir = temp_dir / ".gobby"
        gobby_dir.mkdir(exist_ok=True)
        (gobby_dir / "project.json").write_text('{"id": "context-project-id", "name": "test"}')

        result = manager._resolve_project_id(None, str(temp_dir))
        assert result == "context-project-id"

    def test_resolve_project_id_raises_without_project_json(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that ValueError is raised when no project.json exists."""
        manager = hook_manager_with_mocks

        # Create a new temp dir without project.json
        new_dir = temp_dir / "new_project"
        new_dir.mkdir()

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with pytest.raises(ValueError, match="gobby init"):
                manager._resolve_project_id(None, str(new_dir))


class TestHookManagerLogging:
    """Tests for logging setup."""

    def test_setup_logging_creates_log_directory(
        self,
        temp_dir: Path,
        mock_daemon_client: MagicMock,
        hub_db: HubDatabase,
    ) -> None:
        """Test that logging setup creates the log file directory."""
        # First ensure the parent directory for logs doesn't exist
        log_dir = temp_dir / "new_custom_logs"
        log_path = log_dir / "hook.log"

        # Verify it doesn't exist
        assert not log_dir.exists()

        with patch("gobby.hooks.factory.DaemonClient") as MockDaemonClient:
            MockDaemonClient.return_value = mock_daemon_client

            manager = HookManager(
                daemon_host="localhost",
                daemon_port=60887,
                database=hub_db,
                log_file=str(log_path),
            )

            # Log directory should be created (as part of _setup_logging)
            # Note: The logger creates the directory when initializing the file handler
            assert manager.log_file == str(log_path)
            assert manager.logger is not None

            manager.shutdown()

    def test_setup_logging_reuses_existing_logger(
        self,
        temp_dir: Path,
        mock_daemon_client: MagicMock,
        hub_db: HubDatabase,
    ) -> None:
        """Test that logging setup reuses existing logger if already configured."""
        import logging

        # Pre-configure the logger with a handler
        logger = logging.getLogger("gobby.hooks")
        handler = logging.StreamHandler()
        logger.addHandler(handler)

        with patch("gobby.hooks.factory.DaemonClient") as MockDaemonClient:
            MockDaemonClient.return_value = mock_daemon_client

            manager = HookManager(
                daemon_host="localhost",
                daemon_port=60887,
                database=hub_db,
                log_file=str(temp_dir / "logs" / "hook.log"),
            )

            # Logger should be returned without adding duplicate handlers
            assert manager.logger is not None

            manager.shutdown()

        # Cleanup
        logger.removeHandler(handler)


class TestHookManagerContextMerging:
    """Tests for context merging between workflow and response."""

    def test_merge_workflow_context_with_existing_response_context(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that workflow context is appended to existing response context."""
        manager = hook_manager_with_mocks

        # Mock workflow handler to return context
        workflow_response = HookResponse(decision="allow", context="Workflow context")

        # Mock event handler to return response with context
        def handler_with_context(event):
            return HookResponse(decision="allow", context="Handler context")

        with (
            patch.object(manager._workflow_handler, "handle", return_value=workflow_response),
            patch.object(manager._event_handlers, "get_handler", return_value=handler_with_context),
        ):
            response = manager.handle(sample_session_start_event)

        # Both contexts should be present
        assert "Handler context" in response.context
        assert "Workflow context" in response.context


class TestHookManagerMachineIdFallback:
    """Tests for machine ID fallback behavior."""

    def test_get_machine_id_returns_unknown_on_none(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that get_machine_id returns 'unknown-machine' when underlying returns None."""
        manager = hook_manager_with_mocks

        with patch("gobby.utils.machine_id.get_machine_id", return_value=None):
            # Since we can't easily mock the import inside the method,
            # we verify the fallback logic exists by checking the return type
            result = manager.get_machine_id()
            assert isinstance(result, str)
            # When underlying returns None, should return "unknown-machine"
            assert result == "unknown-machine"

    def test_get_machine_id_returns_value_when_available(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that get_machine_id returns the underlying value when available."""
        manager = hook_manager_with_mocks

        with patch("gobby.utils.machine_id.get_machine_id", return_value="my-machine-id"):
            result = manager.get_machine_id()
            assert result == "my-machine-id"
