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

import asyncio
import json
import logging
import threading
import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# This import should fail initially (red phase) - module doesn't exist yet
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

# projects.id, sessions.id, and workflow instance ids are native uuid columns.
PROJECT_ID = "eeeeeeee-0000-4000-8000-000000000001"
PARENT_SESSION_ID = "eeeeeeee-0000-4000-8000-000000000002"
CHILD_SESSION_ID = "eeeeeeee-0000-4000-8000-000000000003"


def _create_session_row(db: HubDatabase, session_id: str) -> None:
    db.execute(
        """
        INSERT INTO projects (id, name, created_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        (PROJECT_ID, "test-project"),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (
            session_id,
            f"ext-{session_id}",
            "21000000-0000-4000-8000-000000000001",
            "claude",
            PROJECT_ID,
        ),
    )


def _install_step_workflow(db: HubDatabase, session_id: str, current_step: str) -> None:
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
    from gobby.workflows.agent_models import AgentStepWorkflowBody
    from gobby.workflows.definitions import WorkflowStep
    from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager

    definition = {
        "name": "merge-worker",
        "version": "1.0",
        "enabled": True,
        "steps": [
            {"name": "resolve_conflicts"},
            {"name": "terminate"},
        ],
        "exit_condition": "current_step == 'terminate'",
    }
    LocalWorkflowDefinitionManager(db).create(
        name="merge-worker",
        definition_json=json.dumps(definition),
        workflow_type="workflow",
        enabled=True,
    )
    AgentStepInstanceManager(db).save(
        AgentStepInstance(
            # workflow_instances.id is a native uuid column.
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"inst-{session_id}")),
            session_id=session_id,
            agent_name="merge-worker",
            snapshot=AgentStepWorkflowBody(steps=[WorkflowStep(name=current_step)]),
            current_step=current_step,
            variables={},
        )
    )


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
        coordinator.unregister_session("nonexistent")
        assert coordinator._registered_sessions == set()

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

        cached_message, timestamp = coordinator._agent_message_cache["session-123"]
        coordinator._agent_message_cache["session-123"] = (cached_message, timestamp - 1.1)
        message = coordinator.get_cached_message("session-123", max_age_seconds=1.0)
        assert message is None


class TestSessionLifecycleTransitions:
    """Test session lifecycle transitions."""

    def test_reregister_active_sessions(self) -> None:
        """Test re-registering active sessions from storage."""
        mock_session_storage = MagicMock()
        mock_session_storage.list.side_effect = lambda status, limit: {
            "active": [
                MagicMock(
                    id="session-1",
                    transcript_path="/path/to/1.jsonl",
                    source="claude",
                    transcript_processed=False,
                ),
                MagicMock(
                    id="session-2",
                    transcript_path="/path/to/2.jsonl",
                    source="qwen",
                    transcript_processed=False,
                ),
            ],
            "paused": [],
        }[status]

        mock_message_processor = MagicMock()

        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor_resolver=lambda: mock_message_processor,
        )

        count = coordinator.reregister_active_sessions()

        assert count == 2
        assert mock_message_processor.register_session.call_count == 2

    def test_reregister_resolves_current_processor_and_disabled_tracking_noops(self) -> None:
        session = MagicMock(
            id="session-1",
            transcript_path="/path/to/1.jsonl",
            source="claude",
        )
        session_storage = MagicMock()
        session_storage.list.side_effect = lambda status, limit: (
            [session] if status == "active" else []
        )
        old_processor = MagicMock()
        rebuilt_processor = MagicMock()
        current: list[Any | None] = [old_processor]
        coordinator = SessionCoordinator(
            session_storage=session_storage,
            message_processor_resolver=lambda: current[0],
        )

        current[0] = rebuilt_processor
        assert coordinator.reregister_active_sessions() == 1
        rebuilt_processor.register_session.assert_called_once_with(
            "session-1", "/path/to/1.jsonl", source="claude"
        )
        old_processor.register_session.assert_not_called()

        current[0] = None
        assert coordinator.reregister_active_sessions() == 0
        assert rebuilt_processor.register_session.call_count == 1

    def test_reregister_active_session_with_processed_transcript(self) -> None:
        """Active transcripts remain live after their last completed processing pass."""
        active_session = MagicMock(
            id="session-1",
            transcript_path="/path/to/1.jsonl",
            source="codex",
            transcript_processed=True,
        )
        mock_session_storage = MagicMock()
        mock_session_storage.list.side_effect = lambda status, limit: {
            "active": [active_session],
            "paused": [],
        }[status]
        mock_message_processor = MagicMock()
        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor_resolver=lambda: mock_message_processor,
        )

        count = coordinator.reregister_active_sessions()

        assert count == 1
        mock_message_processor.register_session.assert_called_once_with(
            "session-1", "/path/to/1.jsonl", source="codex"
        )

    def test_reregister_includes_paused_sessions(self) -> None:
        """Test re-registration includes paused sessions."""
        mock_session_storage = MagicMock()
        mock_session_storage.list.side_effect = lambda status, limit: {
            "active": [
                MagicMock(
                    id="session-1",
                    transcript_path="/path/to/1.jsonl",
                    source="claude",
                    transcript_processed=False,
                ),
            ],
            "paused": [
                MagicMock(
                    id="session-2",
                    transcript_path="/path/to/2.jsonl",
                    source="claude",
                    transcript_processed=False,
                ),
            ],
        }[status]

        mock_message_processor = MagicMock()

        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor_resolver=lambda: mock_message_processor,
        )

        count = coordinator.reregister_active_sessions()

        assert count == 2
        mock_message_processor.register_session.assert_any_call(
            "session-2", "/path/to/2.jsonl", source="claude"
        )
        assert mock_message_processor.register_session.call_count >= 1
        assert mock_message_processor.register_session.call_args is not None

    def test_reregister_skips_sessions_without_transcript_path(self) -> None:
        """Test re-registration skips sessions without transcript_path."""
        mock_session_storage = MagicMock()
        mock_session_storage.list.side_effect = lambda status, limit: {
            "active": [MagicMock(id="session-1", transcript_path=None, source="claude")],
            "paused": [],
        }[status]

        mock_message_processor = MagicMock()

        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor_resolver=lambda: mock_message_processor,
        )

        count = coordinator.reregister_active_sessions()

        assert count == 0
        mock_message_processor.register_session.assert_not_called()

    def test_reregister_handles_errors_gracefully(self) -> None:
        """Test re-registration handles individual session errors."""
        mock_session_storage = MagicMock()
        mock_session_storage.list.side_effect = lambda status, limit: {
            "active": [
                MagicMock(
                    id="session-1",
                    transcript_path="/path/1.jsonl",
                    source="claude",
                    transcript_processed=False,
                ),
                MagicMock(
                    id="session-2",
                    transcript_path="/path/2.jsonl",
                    source="claude",
                    transcript_processed=False,
                ),
            ],
            "paused": [],
        }[status]

        mock_message_processor = MagicMock()
        mock_message_processor.register_session.side_effect = [
            Exception("Error"),
            None,  # Second call succeeds
        ]

        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor_resolver=lambda: mock_message_processor,
            logger=logging.getLogger("test"),
        )

        count = coordinator.reregister_active_sessions()

        # Should still count the successful one
        assert count == 1
        assert mock_message_processor.register_session.call_count == 2

    def test_reregister_logs_storage_failure_with_structured_context(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_session_storage = MagicMock()
        mock_session_storage.list.side_effect = RuntimeError("storage unavailable")
        logger = logging.getLogger("test.session_coordinator.structured")
        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor_resolver=lambda: MagicMock(),
            logger=logger,
        )

        with caplog.at_level("WARNING", logger=logger.name):
            count = coordinator.reregister_active_sessions()

        assert count == 0
        record = next(
            record
            for record in caplog.records
            if record.getMessage() == "Failed to re-register active/paused sessions"
        )
        assert record.__dict__["error"] == "storage unavailable"

    def test_reregister_does_not_reset_agent_context_flags(self) -> None:
        """Test re-registration only restores transcript processing."""
        mock_session_storage = MagicMock()
        mock_session_storage.list.side_effect = lambda status, limit: {
            "active": [
                MagicMock(
                    id="session-1",
                    transcript_path="/path/to/1.jsonl",
                    source="claude",
                    transcript_processed=False,
                ),
            ],
            "paused": [],
        }[status]

        mock_message_processor = MagicMock()

        coordinator = SessionCoordinator(
            session_storage=mock_session_storage,
            message_processor_resolver=lambda: mock_message_processor,
        )

        with patch("gobby.workflows.state_manager.SessionVariableManager") as MockSVMgr:
            count = coordinator.reregister_active_sessions()

        assert count == 1
        assert mock_session_storage.list.call_args_list == [
            call(status="active", limit=1000),
            call(status="paused", limit=1000),
        ]
        mock_message_processor.register_session.assert_called_once_with(
            "session-1",
            "/path/to/1.jsonl",
            source="claude",
        )
        assert mock_message_processor.register_session.call_count == 1
        MockSVMgr.assert_not_called()


class TestAgentRunCompletion:
    """Test agent run completion logic."""

    @patch("gobby.agents.tmux.get_configured_tmux_command_prefix", side_effect=lambda: ["tmux"])
    @patch("subprocess.run")
    def test_complete_agent_run_captures_full_tmux_history(
        self,
        mock_run: MagicMock,
        _mock_tmux_prefix: MagicMock,
    ) -> None:
        large_output = "old" * 20_000 + "newest"
        killed = False

        def run_tmux(command: list[str], **_kwargs: object) -> SimpleNamespace:
            nonlocal killed
            if "capture-pane" in command:
                return SimpleNamespace(returncode=0, stdout=large_output)
            if "kill-session" in command:
                killed = True
                return SimpleNamespace(returncode=0, stdout="")
            return SimpleNamespace(returncode=int(killed), stdout="")

        mock_run.side_effect = run_tmux
        mock_agent_run_manager = MagicMock()
        mock_agent_run_manager.db.fetchone.return_value = None
        running = SimpleNamespace(
            id="run-id",
            result=None,
            capture_id=None,
            capture_revision=0,
            status="running",
            tmux_session_name="agent-run",
        )
        persisted = SimpleNamespace(**{**vars(running), "result": large_output})
        mock_agent_run_manager.get.return_value = running
        mock_agent_run_manager.record_termination_intent.return_value = running
        mock_agent_run_manager.replace_capture_slot.return_value = persisted
        mock_agent_run_manager.complete.return_value = SimpleNamespace(status="completed")
        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)
        session = SimpleNamespace(
            id="session-id",
            agent_run_id="run-id",
            summary_markdown=None,
            last_assistant_content=None,
            tool_call_count=1,
            turn_count=1,
        )

        coordinator.complete_agent_run(session)

        captured = mock_agent_run_manager.replace_capture_slot.call_args.kwargs["slot_content"]
        assert large_output in captured
        assert "newest" in captured
        capture_command = next(
            call.args[0] for call in mock_run.call_args_list if "capture-pane" in call.args[0]
        )
        assert capture_command[-2:] == ["-S", "-"]

    @pytest.mark.asyncio
    async def test_complete_agent_run_flushes_stats_before_refresh(self) -> None:
        """A short run uses stats and result persisted by the awaited flush."""
        agent_run_manager = MagicMock()
        agent_run_manager.get.return_value = MagicMock(status="running", tmux_session_name=None)
        agent_run_manager.db.fetchone.return_value = None
        message_processor = MagicMock()
        stale_processor = MagicMock()
        current = [stale_processor]
        flush_completed = False

        async def flush_session(session_id: str) -> None:
            nonlocal flush_completed
            assert session_id == "sess-short"
            flush_completed = True

        message_processor.flush_session = AsyncMock(side_effect=flush_session)
        refreshed_session = SimpleNamespace(
            id="sess-short",
            agent_run_id="run-short",
            summary_markdown="Fresh summary",
            last_assistant_content="",
            tool_call_count=2,
            turn_count=1,
        )
        session_manager = MagicMock()

        def get_refreshed_session(session_id: str) -> SimpleNamespace:
            assert session_id == "sess-short"
            assert flush_completed is True
            return refreshed_session

        session_manager.get.side_effect = get_refreshed_session
        coordinator = SessionCoordinator(
            session_storage=session_manager,
            message_processor_resolver=lambda: current[0],
            agent_run_manager=agent_run_manager,
        )
        coordinator.set_completion_registry(MagicMock())
        original_session = SimpleNamespace(
            id="sess-short",
            agent_run_id="run-short",
            summary_markdown="Stale summary",
            last_assistant_content="",
            tool_call_count=0,
            turn_count=0,
        )
        current[0] = message_processor

        await asyncio.to_thread(coordinator.complete_agent_run, original_session)

        message_processor.flush_session.assert_awaited_once_with("sess-short")
        stale_processor.flush_session.assert_not_called()
        agent_run_manager.fail.assert_not_called()
        agent_run_manager.complete.assert_called_once_with(
            run_id="run-short",
            result="Fresh summary",
            tool_calls_count=2,
            turns_used=1,
        )

    def test_complete_agent_run_updates_status(self) -> None:
        """Test completing an agent run updates its status."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="running")
        mock_agent_run_manager.get.return_value = mock_agent_run

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = "run-123"
        mock_session.summary_markdown = "Summary"

        coordinator.complete_agent_run(mock_session)

        mock_agent_run_manager.complete.assert_called_once()
        call_kwargs = mock_agent_run_manager.complete.call_args[1]
        assert call_kwargs["run_id"] == "run-123"
        assert call_kwargs["result"] == "Summary"

    def test_complete_agent_run_uses_latest_inter_session_message(
        self, temp_db: HubDatabase
    ) -> None:
        session_id = str(uuid.uuid4())
        recipient_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        _create_session_row(temp_db, session_id)
        _create_session_row(temp_db, recipient_id)
        temp_db.executemany(
            """
            INSERT INTO inter_session_messages
                (id, from_session, to_session, content, priority, sent_at)
            VALUES (%s, %s, %s, %s, 'normal', %s)
            """,
            [
                (
                    str(uuid.uuid4()),
                    session_id,
                    recipient_id,
                    "newest result",
                    "2026-01-02T00:00:00+00:00",
                ),
                (
                    str(uuid.uuid4()),
                    session_id,
                    recipient_id,
                    "older result",
                    "2026-01-01T00:00:00+00:00",
                ),
            ],
        )
        mock_agent_run_manager = MagicMock(db=temp_db)
        mock_agent_run_manager.get.return_value = MagicMock(
            status="running", tmux_session_name=None
        )
        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)
        session = SimpleNamespace(
            id=session_id,
            agent_run_id=run_id,
            summary_markdown=None,
            last_assistant_content=None,
            tool_call_count=1,
            turn_count=1,
        )

        coordinator.complete_agent_run(session)

        assert mock_agent_run_manager.complete.call_args.kwargs["result"] == "newest result"

    def test_complete_agent_run_notifies_stored_status_when_complete_loses_race(self) -> None:
        mock_agent_run_manager = MagicMock()
        running_run = MagicMock(status="running", tmux_session_name=None)
        terminal_run = MagicMock(status="cancelled")
        mock_agent_run_manager.get.side_effect = [running_run, terminal_run]
        mock_agent_run_manager.complete.return_value = None
        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)
        notify = MagicMock()
        session = MagicMock(
            id="session-123",
            agent_run_id="run-123",
            summary_markdown="Summary",
            tool_call_count=1,
            turn_count=1,
        )

        with patch.object(coordinator, "_notify_agent_completion", notify):
            coordinator.complete_agent_run(session)

        notify.assert_called_once_with("run-123", "cancelled")
        assert mock_agent_run_manager.get.call_count == 2
        assert mock_agent_run_manager.complete.call_count == 1
        assert mock_agent_run_manager.fail.call_count == 0
        assert notify.call_count == 1

    def test_complete_agent_run_notifies_stored_status_when_fail_loses_race(self) -> None:
        mock_agent_run_manager = MagicMock()
        running_run = MagicMock(status="running", tmux_session_name=None)
        terminal_run = MagicMock(status="success")
        mock_agent_run_manager.get.side_effect = [running_run, terminal_run]
        mock_agent_run_manager.fail.return_value = None
        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)
        notify = MagicMock()
        session = MagicMock(
            id="session-123",
            agent_run_id="run-123",
            summary_markdown="Summary",
            tool_call_count=0,
            turn_count=0,
        )

        with patch.object(coordinator, "_notify_agent_completion", notify):
            coordinator.complete_agent_run(session)

        notify.assert_called_once_with("run-123", "success")
        assert mock_agent_run_manager.get.call_count == 2
        assert mock_agent_run_manager.fail.call_count == 1
        assert mock_agent_run_manager.complete.call_count == 0
        assert notify.call_count == 1

    def test_timed_out_terminal_offload_runs_followups_after_persistence(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = MagicMock()
        terminal_run = MagicMock(status="success")
        manager.complete.return_value = terminal_run
        coordinator = SessionCoordinator(agent_run_manager=manager)
        mock_notify = MagicMock()
        mock_release = MagicMock()
        submitted: dict[str, Any] = {}

        def submit(operation: Any, **kwargs: Any) -> MagicMock:
            submitted["operation"] = operation
            submitted["kwargs"] = kwargs
            future = MagicMock()
            future.result.side_effect = TimeoutError("still running")
            submitted["future"] = future
            return future

        with (
            patch.object(coordinator, "_notify_agent_completion", mock_notify),
            patch.object(coordinator, "release_session_worktrees", mock_release),
            patch(
                "gobby.agents.terminal_delivery.submit_terminal_delivery_offload",
                side_effect=submit,
            ),
        ):
            result = coordinator._terminate_agent_run(
                run_id="run-123",
                agent_run=MagicMock(tmux_session_name=None),
                action="complete",
                reason=None,
                result_prefix="result",
                tool_calls_count=2,
                turns_used=1,
                session_id="session-123",
            )

            assert result is None
            cast(MagicMock, submitted["future"]).result.assert_called_once_with(timeout=20.0)
            mock_notify.assert_not_called()
            mock_release.assert_not_called()
            assert "terminal delivery did not finish within 20 seconds" in caplog.text

            updated = submitted["operation"](**submitted["kwargs"])

            assert updated is terminal_run
            mock_notify.assert_called_once_with("run-123", "success")
            mock_release.assert_called_once_with("session-123")

    def test_complete_agent_run_skips_without_run_id(self) -> None:
        """Test complete_agent_run skips sessions without agent_run_id."""
        mock_agent_run_manager = MagicMock()
        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = None

        coordinator.complete_agent_run(mock_session)

        mock_agent_run_manager.complete.assert_not_called()
        assert mock_agent_run_manager.complete.call_count == 0
        assert not mock_agent_run_manager.complete.called

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
        assert mock_agent_run_manager.complete.call_count == 0
        assert not mock_agent_run_manager.complete.called

    def test_complete_agent_run_counts_tool_calls_from_messages(self) -> None:
        """Test completing an agent run counts tool calls and turns from session_messages."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="running")
        mock_agent_run_manager.get.return_value = mock_agent_run

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = "run-456"
        mock_session.id = "sess-789"
        mock_session.summary_markdown = "Done"
        mock_session.tool_call_count = 5
        mock_session.turn_count = 3

        coordinator.complete_agent_run(mock_session)

        call_kwargs = mock_agent_run_manager.complete.call_args[1]
        assert call_kwargs["tool_calls_count"] == 5
        assert call_kwargs["turns_used"] == 3

    def test_complete_agent_run_zero_activity_marks_failed(self) -> None:
        """Agent with 0 tool calls and 0 turns is marked error, not success."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="running")
        mock_agent_run_manager.get.return_value = mock_agent_run

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = "run-ghost"
        mock_session.id = "sess-ghost"
        mock_session.summary_markdown = ""
        mock_session.tool_call_count = 0
        mock_session.turn_count = 0

        coordinator.complete_agent_run(mock_session)

        mock_agent_run_manager.fail.assert_called_once()
        fail_kwargs = mock_agent_run_manager.fail.call_args[1]
        assert fail_kwargs["run_id"] == "run-ghost"
        assert "no activity" in fail_kwargs["error"].lower()
        mock_agent_run_manager.complete.assert_not_called()

    def test_complete_agent_run_zero_activity_reports_auth_prompt(self) -> None:
        """Zero-activity failures include auth/trust diagnostics from pane output."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="running")
        mock_agent_run_manager.get.return_value = mock_agent_run

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = "run-auth"
        mock_session.id = "sess-auth"
        mock_session.summary_markdown = "Claude Code\n/login\n"
        mock_session.tool_call_count = 0
        mock_session.turn_count = 0

        coordinator.complete_agent_run(mock_session)

        fail_kwargs = mock_agent_run_manager.fail.call_args[1]
        assert "auth/trust prompt detected" in fail_kwargs["error"]
        assert "no activity" in fail_kwargs["error"].lower()
        mock_agent_run_manager.complete.assert_not_called()

    def test_complete_agent_run_fails_incomplete_step_workflow(
        self,
        temp_db: HubDatabase,
    ) -> None:
        """SESSION_END does not mark a live step workflow as successful."""
        from gobby.storage.agents import LocalAgentRunManager

        _create_session_row(temp_db, PARENT_SESSION_ID)
        _create_session_row(temp_db, CHILD_SESSION_ID)
        _install_step_workflow(temp_db, CHILD_SESSION_ID, "resolve_conflicts")

        run_manager = LocalAgentRunManager(temp_db)
        run = run_manager.create(
            parent_session_id=PARENT_SESSION_ID,
            provider="claude",
            prompt="resolve merge conflicts",
            workflow_name="merge-worker",
            agent_name="merge-worker",
            snapshot=AgentStepWorkflowBody(steps=[WorkflowStep(name=current_step)]),
            child_session_id=CHILD_SESSION_ID,
        )
        run_manager.start(run.id)

        coordinator = SessionCoordinator(agent_run_manager=run_manager)
        session = SimpleNamespace(
            id=CHILD_SESSION_ID,
            agent_run_id=run.id,
            summary_markdown="Blocked by step enforcement.",
            tool_call_count=7,
            turn_count=3,
        )

        coordinator.complete_agent_run(session)

        updated = run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "error"
        assert updated.error is not None
        assert "before step workflow completed" in updated.error
        assert "workflow=merge-worker" in updated.error
        assert "current_step=resolve_conflicts" in updated.error

    def test_complete_agent_run_allows_completed_step_workflow(
        self,
        temp_db: HubDatabase,
    ) -> None:
        """A leftover terminal-step workflow instance does not force failure."""
        from gobby.storage.agents import LocalAgentRunManager

        _create_session_row(temp_db, PARENT_SESSION_ID)
        _create_session_row(temp_db, CHILD_SESSION_ID)
        _install_step_workflow(temp_db, CHILD_SESSION_ID, "terminate")

        run_manager = LocalAgentRunManager(temp_db)
        run = run_manager.create(
            parent_session_id=PARENT_SESSION_ID,
            provider="claude",
            prompt="resolve merge conflicts",
            workflow_name="merge-worker",
            agent_name="merge-worker",
            snapshot=AgentStepWorkflowBody(steps=[WorkflowStep(name=current_step)]),
            child_session_id=CHILD_SESSION_ID,
        )
        run_manager.start(run.id)

        coordinator = SessionCoordinator(agent_run_manager=run_manager)
        session = SimpleNamespace(
            id=CHILD_SESSION_ID,
            agent_run_id=run.id,
            summary_markdown="Done",
            tool_call_count=7,
            turn_count=3,
        )

        coordinator.complete_agent_run(session)

        updated = run_manager.get(run.id)
        assert updated is not None
        assert updated.status == "success"
        assert updated.error is None

    def test_complete_agent_run_defaults_counts_when_missing(self) -> None:
        """Stats attributes from session are passed through to complete()."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="running")
        mock_agent_run_manager.get.return_value = mock_agent_run

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        mock_session = MagicMock()
        mock_session.agent_run_id = "run-456"
        mock_session.id = "sess-789"
        mock_session.summary_markdown = "Done"
        mock_session.tool_call_count = 10
        mock_session.turn_count = 5

        coordinator.complete_agent_run(mock_session)

        mock_agent_run_manager.complete.assert_called_once()
        call_kwargs = mock_agent_run_manager.complete.call_args[1]
        assert call_kwargs["tool_calls_count"] == 10
        assert call_kwargs["turns_used"] == 5


class TestStartAgentRunIdempotency:
    """Pin SessionCoordinator.start_agent_run's pending-gate behavior.

    spawn_agent_impl flips status to 'running' at spawn time. The child
    session's SessionStart hook later calls start_agent_run too — it must
    be a safe no-op in that case. If this guard regressed to unconditionally
    calling manager.start(), we'd double-bump started_at and risk clobbering
    a run that's already progressed (e.g. completed).
    """

    def test_start_agent_run_transitions_pending_to_running(self) -> None:
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="pending")
        mock_agent_run_manager.get.return_value = mock_agent_run

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        assert coordinator.start_agent_run("run-abc") is True
        mock_agent_run_manager.start.assert_called_once_with("run-abc")

    def test_start_agent_run_is_noop_when_already_running(self) -> None:
        """Second call (e.g. from SessionStart hook after spawn_agent_impl
        already flipped status) must not re-invoke manager.start()."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run = MagicMock(status="running")
        mock_agent_run_manager.get.return_value = mock_agent_run

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        assert coordinator.start_agent_run("run-abc") is False
        mock_agent_run_manager.start.assert_not_called()

    @pytest.mark.parametrize("terminal", ("success", "failed", "cancelled", "timeout", "error"))
    def test_start_agent_run_is_noop_for_terminal_states(self, terminal: str) -> None:
        """Runs that already completed/failed/cancelled must not be restarted
        by a late hook fire."""
        mock_agent_run_manager = MagicMock()
        mock_agent_run_manager.get.return_value = MagicMock(status=terminal)
        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        assert coordinator.start_agent_run("run-abc") is False, terminal
        mock_agent_run_manager.start.assert_not_called()

    def test_start_agent_run_returns_false_for_unknown_run(self) -> None:
        mock_agent_run_manager = MagicMock()
        mock_agent_run_manager.get.return_value = None

        coordinator = SessionCoordinator(agent_run_manager=mock_agent_run_manager)

        assert coordinator.start_agent_run("run-missing") is False
        mock_agent_run_manager.start.assert_not_called()


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
        assert mock_worktree_manager.release.call_count == 0
        assert not mock_worktree_manager.release.called

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

        def register_sessions() -> Any:
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

        def cache_messages() -> Any:
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

        def increment_with_lock() -> Any:
            with coordinator.get_lookup_lock("external-1", "claude"):
                # Simulate work
                current = call_count["count"]
                threading.Event().wait(0.01)
                call_count["count"] = current + 1

        threads = [threading.Thread(target=increment_with_lock) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # With proper locking, all increments should be serialized
        assert call_count["count"] == 10

    def test_lookup_locks_are_keyed_by_external_id_and_source(self) -> None:
        coordinator = SessionCoordinator()

        first = coordinator.get_lookup_lock("external-1", "claude")

        assert coordinator.get_lookup_lock("external-1", "claude") is first
        assert coordinator.get_lookup_lock("external-2", "claude") is not first
        assert coordinator.get_lookup_lock("external-1", "codex") is not first


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
            message_processor_resolver=lambda: mock_message_processor,
            agent_run_manager=mock_agent_run_manager,
            worktree_manager=mock_worktree_manager,
            logger=logger,
        )

        assert coordinator._session_manager is mock_session_storage
        assert coordinator._message_processor_resolver() is mock_message_processor
        assert coordinator._agent_run_manager is mock_agent_run_manager
        assert coordinator._worktree_manager is mock_worktree_manager
        assert coordinator.logger is logger

    def test_init_without_dependencies(self) -> None:
        """Test initialization without dependencies (graceful degradation)."""
        coordinator = SessionCoordinator()

        assert coordinator._session_manager is None
        assert coordinator._message_processor_resolver() is None
        assert coordinator._agent_run_manager is None
        assert coordinator._worktree_manager is None
        assert coordinator.logger is not None

    def test_init_creates_locks(self) -> None:
        """Test initialization creates all required locks."""
        coordinator = SessionCoordinator()

        assert hasattr(coordinator, "_registered_sessions_lock")
        assert hasattr(coordinator, "_cache_lock")
        assert hasattr(coordinator, "_lookup_locks")
        assert hasattr(coordinator, "_lookup_locks_lock")


class TestIntegrationWithHookManager:
    """Test integration patterns with HookManager."""

    def test_can_be_used_as_component(self) -> None:
        """Test SessionCoordinator can be composed into HookManager."""

        class MockHookManager:
            """Simulates how HookManager would use SessionCoordinator."""

            def __init__(self) -> None:
                self._session_coordinator = SessionCoordinator()

            def handle_session_start(self, session_id: str) -> Any:
                if not self._session_coordinator.is_registered(session_id):
                    self._session_coordinator.register_session(session_id)
                    return "Session registered"
                return "Already registered"

            def handle_session_end(self, session: Any) -> Any:
                self._session_coordinator.complete_agent_run(session)
                self._session_coordinator.unregister_session(session.id)

        manager = MockHookManager()

        # First registration
        result = manager.handle_session_start("session-123")
        assert result == "Session registered"

        # Duplicate registration
        result = manager.handle_session_start("session-123")
        assert result == "Already registered"


class _GatedRegistry:
    """Recording completion-registry fake whose notify blocks until released."""

    def __init__(self, delivery: dict[str, bool] | None, release: asyncio.Event) -> None:
        self._delivery = delivery
        self._release = release
        self.notify_calls: list[tuple[str, dict[str, Any] | None, str]] = []
        self.cleanup_calls: list[str] = []
        self.order: list[str] = []
        self.started = asyncio.Event()

    async def notify(
        self, run_id: str, *, result: dict[str, Any] | None = None, message: str = ""
    ) -> dict[str, bool] | None:
        self.notify_calls.append((run_id, result, message))
        self.started.set()
        await self._release.wait()
        self.order.append("notify-resolved")
        return self._delivery

    def cleanup(self, run_id: str) -> None:
        self.cleanup_calls.append(run_id)
        self.order.append("cleanup")


class TestNotifyAgentCompletionDelivery:
    """Plan 1.4.6: the coordinator schedules the acknowledged helper chain."""

    def _coordinator(self, registry: _GatedRegistry) -> SessionCoordinator:
        coordinator = SessionCoordinator()
        coordinator._completion_registry = registry
        coordinator._agent_run_manager = cast(
            LocalAgentRunManager,
            SimpleNamespace(db=MagicMock()),
        )
        return coordinator

    def _record_removals(
        self, monkeypatch: pytest.MonkeyPatch, registry: _GatedRegistry
    ) -> list[tuple[str, list[str] | None]]:
        import gobby.agents.completion_subscribers as subscribers_module

        removals: list[tuple[str, list[str] | None]] = []

        def _record(*, db: Any, run_id: str, session_ids: list[str] | None = None) -> None:
            registry.order.append("remove")
            removals.append((run_id, session_ids))

        monkeypatch.setattr(subscribers_module, "remove_agent_completion_subscribers", _record)
        return removals

    def _schedule(self, coordinator: SessionCoordinator, run_id: str) -> asyncio.Task[Any]:
        before = asyncio.all_tasks()
        coordinator._notify_agent_completion(run_id, "completed")
        new_tasks = asyncio.all_tasks() - before
        assert len(new_tasks) == 1
        return new_tasks.pop()

    @pytest.mark.asyncio
    async def test_cleanup_runs_only_after_awaited_notify_and_delivered_map(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = asyncio.Event()
        registry = _GatedRegistry({"sess-1": True, "sess-2": False}, release)
        removals = self._record_removals(monkeypatch, registry)
        coordinator = self._coordinator(registry)

        task = self._schedule(coordinator, "run-1")
        await registry.started.wait()
        assert registry.notify_calls[0][1] == {"status": "completed", "run_id": "run-1"}
        assert removals == []
        assert registry.cleanup_calls == []

        release.set()
        await task
        assert removals == [("run-1", ["sess-1"])]
        assert registry.cleanup_calls == ["run-1"]
        assert registry.order == ["notify-resolved", "remove", "cleanup"]

    @pytest.mark.asyncio
    async def test_duplicate_notify_without_map_removes_no_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = asyncio.Event()
        release.set()
        registry = _GatedRegistry(None, release)
        removals = self._record_removals(monkeypatch, registry)
        coordinator = self._coordinator(registry)

        await self._schedule(coordinator, "run-1")
        assert removals == []
        assert registry.cleanup_calls == ["run-1"]

    @pytest.mark.asyncio
    async def test_current_loop_cancellation_before_delivery_retains_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = asyncio.Event()
        registry = _GatedRegistry({"sess-1": True}, release)
        removals = self._record_removals(monkeypatch, registry)
        coordinator = self._coordinator(registry)

        task = self._schedule(coordinator, "run-1")
        await registry.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert removals == []
        assert registry.cleanup_calls == []

    @pytest.mark.asyncio
    async def test_cross_thread_branch_delivers_and_settles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = asyncio.Event()
        registry = _GatedRegistry({"sess-1": True}, release)
        removals = self._record_removals(monkeypatch, registry)
        coordinator = self._coordinator(registry)
        coordinator._event_loop = asyncio.get_running_loop()

        before = asyncio.all_tasks()
        await asyncio.to_thread(coordinator._notify_agent_completion, "run-1", "completed")
        await registry.started.wait()
        new_tasks = asyncio.all_tasks() - before
        assert len(new_tasks) == 1
        task = new_tasks.pop()

        release.set()
        await task
        assert removals == [("run-1", ["sess-1"])]
        assert registry.cleanup_calls == ["run-1"]

    @pytest.mark.asyncio
    async def test_cross_thread_cancellation_before_delivery_retains_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = asyncio.Event()
        registry = _GatedRegistry({"sess-1": True}, release)
        removals = self._record_removals(monkeypatch, registry)
        coordinator = self._coordinator(registry)
        coordinator._event_loop = asyncio.get_running_loop()

        before = asyncio.all_tasks()
        await asyncio.to_thread(coordinator._notify_agent_completion, "run-1", "completed")
        await registry.started.wait()
        task = (asyncio.all_tasks() - before).pop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert removals == []
        assert registry.cleanup_calls == []

    @pytest.mark.asyncio
    async def test_absent_or_closed_loop_skips_delivery_and_retains_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = asyncio.Event()
        registry = _GatedRegistry({"sess-1": True}, release)
        removals = self._record_removals(monkeypatch, registry)
        coordinator = self._coordinator(registry)
        coordinator._event_loop = None

        await asyncio.to_thread(coordinator._notify_agent_completion, "run-1", "completed")
        assert registry.notify_calls == []
        assert removals == []
        assert registry.cleanup_calls == []


class _AlreadyTerminalRunStorage:
    """Capture-storage stub for a run that self-terminated before the hook."""

    def __init__(self, run: Any) -> None:
        self._run = run

    def get(self, run_id: str) -> Any | None:
        return self._run if run_id == self._run.id else None

    def record_termination_intent(self, run_id: str, **_kwargs: Any) -> Any | None:
        return None


class TestInlineTerminalizationAlreadyTerminal:
    """Deferred terminalization after self-termination is a benign skip."""

    def test_already_terminal_logs_info_not_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        from datetime import UTC, datetime

        from gobby.storage.agents import AgentRun

        now = datetime.now(UTC)
        run = AgentRun(
            id="run-self-terminated",
            parent_session_id="parent",
            provider="codex",
            prompt="test",
            status="success",
            created_at=now,
            updated_at=now,
            result="done",
            tmux_session_name="gobby-test-inline",
        )
        storage = _AlreadyTerminalRunStorage(run)
        coordinator = SessionCoordinator(
            agent_run_manager=cast(LocalAgentRunManager, storage),
            logger=logging.getLogger("test.inline_terminalization"),
        )

        with caplog.at_level(logging.INFO, logger="test.inline_terminalization"):
            outcome = coordinator._terminate_agent_run_inline(
                run_id=run.id,
                agent_run=run,
                action="fail",
                reason="session ended",
                result_prefix="",
                tool_calls_count=0,
                turns_used=0,
                session_id="session-inline",
            )

        assert outcome is None
        info_messages = [
            record.message
            for record in caplog.records
            if record.levelno == logging.INFO and "already terminal" in record.message
        ]
        assert info_messages == [
            "Agent run run-self-terminated already terminal; "
            "inline terminalization skipped "
            "(agent run already terminal (status=success))"
        ]
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
