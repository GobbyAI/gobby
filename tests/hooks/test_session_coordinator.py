"""
Tests for SessionCoordinator module (TDD red phase).

These tests are written BEFORE the module exists to drive the extraction
from hook_manager.py. They should initially fail with ImportError.

Test categories:
1. Session registration - Track registered sessions with daemon
2. Session lookup - Find sessions by various keys
3. Session status updates - Track title synthesis and state changes
4. Lifecycle transitions - Complete agent runs, release worktrees
5. Session cleanup - Handle session expiration
6. Concurrent operations - Thread safety
7. State persistence - Cache management
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

# This import should fail initially (red phase) - module doesn't exist yet
from gobby.hooks.session_coordinator import SessionCoordinator

pytestmark = pytest.mark.unit

if TYPE_CHECKING:
    pass


class TestSessionRegistrationTracking:
    """Test session registration tracking."""

    def test_init_creates_empty_registered_set(self) -> None:
        """Test SessionCoordinator starts with empty registered sessions set."""
        coordinator = SessionCoordinator()
        assert coordinator._registered_sessions == set()

    def test_register_session_adds_to_set(self) -> None:
        """Test registering a session adds it to tracking set."""
        coordinator = SessionCoordinator()
        coordinator.register_session("session-123")
        assert "session-123" in coordinator._registered_sessions

    def test_is_registered_returns_true_for_registered(self) -> None:
        """Test is_registered returns True for registered sessions."""
        coordinator = SessionCoordinator()
        coordinator.register_session("session-123")
        assert coordinator.is_registered("session-123") is True

    def test_is_registered_returns_false_for_unregistered(self) -> None:
        """Test is_registered returns False for unregistered sessions."""
        coordinator = SessionCoordinator()
        assert coordinator.is_registered("session-123") is False

    def test_unregister_session_removes_from_set(self) -> None:
        """Test unregistering a session removes it from tracking set."""
        coordinator = SessionCoordinator()
        coordinator.register_session("session-123")
        coordinator.unregister_session("session-123")
        assert "session-123" not in coordinator._registered_sessions

    def test_unregister_nonexistent_is_safe(self) -> None:
        """Test unregistering a non-existent session doesn't raise."""
        coordinator = SessionCoordinator()
        # Should not raise
        coordinator.unregister_session("nonexistent")

    def test_clear_registrations(self) -> None:
        """Test clearing all registrations."""
        coordinator = SessionCoordinator()
        coordinator.register_session("session-1")
        coordinator.register_session("session-2")
        coordinator.clear_registrations()
        assert len(coordinator._registered_sessions) == 0


class TestAgentMessageCache:
    """Test agent message caching between hooks."""

    def test_init_creates_empty_cache(self) -> None:
        """Test SessionCoordinator starts with empty message cache."""
        coordinator = SessionCoordinator()
        assert coordinator._agent_message_cache == {}

    def test_cache_agent_message(self) -> None:
        """Test caching an agent message."""
        coordinator = SessionCoordinator()
        coordinator.cache_agent_message("session-123", "Hello world")
        assert "session-123" in coordinator._agent_message_cache
        message, timestamp = coordinator._agent_message_cache["session-123"]
        assert message == "Hello world"
        assert isinstance(timestamp, float)

    def test_get_cached_message(self) -> None:
        """Test retrieving a cached message."""
        coordinator = SessionCoordinator()
        coordinator.cache_agent_message("session-123", "Hello world")
        message = coordinator.get_cached_message("session-123")
        assert message == "Hello world"

    def test_get_cached_message_returns_none_for_missing(self) -> None:
        """Test get_cached_message returns None for missing session."""
        coordinator = SessionCoordinator()
        assert coordinator.get_cached_message("nonexistent") is None

    def test_clear_cached_message(self) -> None:
        """Test clearing a cached message."""
        coordinator = SessionCoordinator()
        coordinator.cache_agent_message("session-123", "Hello world")
        coordinator.clear_cached_message("session-123")
        assert "session-123" not in coordinator._agent_message_cache

    def test_cached_message_expires(self) -> None:
        """Test that cached messages can have expiration check."""
        coordinator = SessionCoordinator()
        coordinator.cache_agent_message("session-123", "Hello world")

        # Get message with max_age
        message = coordinator.get_cached_message("session-123", max_age_seconds=1.0)
        assert message == "Hello world"

        # Wait for expiration
        time.sleep(1.1)
        message = coordinator.get_cached_message("session-123", max_age_seconds=1.0)
        assert message is None


class TestSessionLifecycleTransitions:
    """Test session lifecycle transitions."""

    def test_reregister_active_sessions(self) -> None:
        """Test re-registering active sessions from storage."""
        mock_session_storage = MagicMock()
        mock_session_storage.list.return_value = [
            MagicMock(id="session-1", jsonl_path="/path/to/1.jsonl", source="claude"),
            MagicMock(id="session-2", jsonl_path="/path/to/2.jsonl", source="gemini"),
        ]

        mock_message_processor = MagicMock()

        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor=mock_message_processor,
        )

        count = coordinator.reregister_active_sessions()

        assert count == 2
        assert mock_message_processor.register_session.call_count == 2

    def test_reregister_skips_sessions_without_jsonl_path(self) -> None:
        """Test re-registration skips sessions without jsonl_path."""
        mock_session_storage = MagicMock()
        mock_session_storage.list.return_value = [
            MagicMock(id="session-1", jsonl_path=None, source="claude"),
        ]

        mock_message_processor = MagicMock()

        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor=mock_message_processor,
        )

        count = coordinator.reregister_active_sessions()

        assert count == 0
        mock_message_processor.register_session.assert_not_called()

    def test_reregister_handles_errors_gracefully(self) -> None:
        """Test re-registration handles individual session errors."""
        mock_session_storage = MagicMock()
        mock_session_storage.list.return_value = [
            MagicMock(id="session-1", jsonl_path="/path/1.jsonl", source="claude"),
            MagicMock(id="session-2", jsonl_path="/path/2.jsonl", source="claude"),
        ]

        mock_message_processor = MagicMock()
        mock_message_processor.register_session.side_effect = [
            Exception("Error"),
            None,  # Second call succeeds
        ]

        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor=mock_message_processor,
            logger=logging.getLogger("test"),
        )

        count = coordinator.reregister_active_sessions()

        # Should still count the successful one
        assert count == 1


class TestAgentRunCompletion:
    """Test agent run completion logic."""

    def test_complete_agent_run_updates_status(self) -> None:
        """Test completing an agent run updates its status."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="running")
        mock_agent_run_manager.get.return_value = mock_agent_run

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = "run-123"
        mock_session.summary_markdown = "Summary"
        mock_session.compact_markdown = None

        coordinator.complete_agent_run(mock_session)

        mock_agent_run_manager.complete.assert_called_once()
        call_kwargs = mock_agent_run_manager.complete.call_args[1]
        assert call_kwargs["run_id"] == "run-123"
        assert call_kwargs["result"] == "Summary"

    def test_complete_agent_run_skips_without_run_id(self) -> None:
        """Test complete_agent_run skips sessions without agent_run_id."""
        mock_agent_run_manager = MagicMock()
        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = None

        coordinator.complete_agent_run(mock_session)

        mock_agent_run_manager.complete.assert_not_called()

    def test_complete_agent_run_skips_terminal_states(self) -> None:
        """Test complete_agent_run skips already-completed runs."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="success")
        mock_agent_run_manager.get.return_value = mock_agent_run

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = "run-123"

        coordinator.complete_agent_run(mock_session)

        mock_agent_run_manager.complete.assert_not_called()

    def test_complete_agent_run_removes_from_running_registry(self) -> None:
        """Test completing an agent run removes it from running registry."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="running")
        mock_agent_run_manager.get.return_value = mock_agent_run

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = "run-123"
        mock_session.summary_markdown = None
        mock_session.compact_markdown = None

        with patch("gobby.agents.registry.get_running_agent_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_get_registry.return_value = mock_registry

            coordinator.complete_agent_run(mock_session)

            mock_registry.remove.assert_called_once_with("run-123")

    def test_complete_agent_run_counts_tool_calls_from_messages(self) -> None:
        """Test completing an agent run counts tool calls and turns from session_messages."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="running")
        mock_agent_run_manager.get.return_value = mock_agent_run

        # Mock DB to return tool call and turn counts
        mock_row = {"tool_calls": 5, "turns": 3}
        mock_agent_run_manager.db.fetchone.return_value = mock_row

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = "run-456"
        mock_session.id = "sess-789"
        mock_session.summary_markdown = "Done"
        mock_session.compact_markdown = None

        coordinator.complete_agent_run(mock_session)

        call_kwargs = mock_agent_run_manager.complete.call_args[1]
        assert call_kwargs["tool_calls_count"] == 5
        assert call_kwargs["turns_used"] == 3

    def test_complete_agent_run_defaults_counts_on_db_error(self) -> None:
        """Test that tool call counts default to 0 if DB query fails."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="running")
        mock_agent_run_manager.get.return_value = mock_agent_run
        mock_agent_run_manager.db.fetchone.side_effect = sqlite3.OperationalError("DB error")

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = "run-456"
        mock_session.id = "sess-789"
        mock_session.summary_markdown = "Done"
        mock_session.compact_markdown = None

        coordinator.complete_agent_run(mock_session)

        call_kwargs = mock_agent_run_manager.complete.call_args[1]
        assert call_kwargs["tool_calls_count"] == 0
        assert call_kwargs["turns_used"] == 0


class TestWorktreeRelease:
    """Test worktree release on session end."""

    def test_release_session_worktrees(self) -> None:
        """Test releasing worktrees when session ends."""
        mock_worktree_manager = MagicMock()
        mock_worktree_manager.list_worktrees.return_value = [
            MagicMock(id="wt-1"),
            MagicMock(id="wt-2"),
        ]

        coordinator = SessionCoordinator(worktree_manager=mock_worktree_manager)

        coordinator.release_session_worktrees("session-123")

        mock_worktree_manager.list_worktrees.assert_called_once_with(agent_session_id="session-123")
        assert mock_worktree_manager.release.call_count == 2

    def test_release_handles_empty_worktrees(self) -> None:
        """Test release handles sessions with no worktrees."""
        mock_worktree_manager = MagicMock()
        mock_worktree_manager.list_worktrees.return_value = []

        coordinator = SessionCoordinator(worktree_manager=mock_worktree_manager)

        # Should not raise
        coordinator.release_session_worktrees("session-123")

        mock_worktree_manager.release.assert_not_called()

    def test_release_handles_individual_errors(self) -> None:
        """Test release handles errors releasing individual worktrees."""
        mock_worktree_manager = MagicMock()
        mock_worktree_manager.list_worktrees.return_value = [
            MagicMock(id="wt-1"),
            MagicMock(id="wt-2"),
        ]
        mock_worktree_manager.release.side_effect = [
            Exception("Error"),
            None,  # Second succeeds
        ]

        coordinator = SessionCoordinator(
            worktree_manager=mock_worktree_manager,
            logger=logging.getLogger("test"),
        )

        # Should not raise, should continue with second worktree
        coordinator.release_session_worktrees("session-123")

        assert mock_worktree_manager.release.call_count == 2


class TestConcurrentOperations:
    """Test thread safety of concurrent operations."""

    def test_registration_thread_safety(self) -> None:
        """Test session registration is thread-safe."""
        coordinator = SessionCoordinator()
        errors: list[Exception] = []

        def register_sessions():
            try:
                for i in range(100):
                    coordinator.register_session(f"session-{threading.current_thread().name}-{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=register_sessions, name=f"t{i}") for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(coordinator._registered_sessions) == 500

    def test_message_cache_thread_safety(self) -> None:
        """Test message caching is thread-safe."""
        coordinator = SessionCoordinator()
        errors: list[Exception] = []

        def cache_messages():
            try:
                for i in range(50):
                    session_id = f"session-{i % 10}"
                    coordinator.cache_agent_message(session_id, f"message-{i}")
                    coordinator.get_cached_message(session_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=cache_messages) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_lookup_lock_prevents_double_firing(self) -> None:
        """Test lookup lock prevents concurrent duplicate operations."""
        coordinator = SessionCoordinator()
        call_count = {"count": 0}

        def increment_with_lock():
            with coordinator.get_lookup_lock():
                # Simulate work
                current = call_count["count"]
                time.sleep(0.01)
                call_count["count"] = current + 1

        threads = [threading.Thread(target=increment_with_lock) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # With proper locking, all increments should be serialized
        assert call_count["count"] == 10


class TestSessionCoordinatorInitialization:
    """Test SessionCoordinator initialization."""

    def test_init_with_all_dependencies(self) -> None:
        """Test initialization with all dependencies."""
        mock_session_storage = MagicMock()
        mock_message_processor = MagicMock()
        mock_agent_run_manager = MagicMock()
        mock_worktree_manager = MagicMock()
        logger = logging.getLogger("test")

        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor=mock_message_processor,
            agent_run_manager=mock_agent_run_manager,
            worktree_manager=mock_worktree_manager,
            logger=logger,
        )

        assert coordinator._session_storage is mock_session_storage
        assert coordinator._message_processor is mock_message_processor
        assert coordinator._agent_run_manager is mock_agent_run_manager
        assert coordinator._worktree_manager is mock_worktree_manager
        assert coordinator.logger is logger

    def test_init_without_dependencies(self) -> None:
        """Test initialization without dependencies (graceful degradation)."""
        coordinator = SessionCoordinator()

        assert coordinator._session_storage is None
        assert coordinator._message_processor is None
        assert coordinator._agent_run_manager is None
        assert coordinator._worktree_manager is None
        assert coordinator.logger is not None

    def test_init_creates_locks(self) -> None:
        """Test initialization creates all required locks."""
        coordinator = SessionCoordinator()

        assert hasattr(coordinator, "_registered_sessions_lock")
        assert hasattr(coordinator, "_cache_lock")
        assert hasattr(coordinator, "_lookup_lock")


class TestIntegrationWithHookManager:
    """Test integration patterns with HookManager."""

    def test_can_be_used_as_component(self) -> None:
        """Test SessionCoordinator can be composed into HookManager."""

        class MockHookManager:
            """Simulates how HookManager would use SessionCoordinator."""

            def __init__(self):
                self._session_coordinator = SessionCoordinator()

            def handle_session_start(self, session_id: str):
                if not self._session_coordinator.is_registered(session_id):
                    self._session_coordinator.register_session(session_id)
                    return "Session registered"
                return "Already registered"

            def handle_session_end(self, session):
                self._session_coordinator.complete_agent_run(session)
                self._session_coordinator.unregister_session(session.id)

        manager = MockHookManager()

        # First registration
        result = manager.handle_session_start("session-123")
        assert result == "Session registered"

        # Duplicate registration
        result = manager.handle_session_start("session-123")
        assert result == "Already registered"
