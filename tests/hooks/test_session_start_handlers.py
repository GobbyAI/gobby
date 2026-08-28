"""Session start handler tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import psycopg
import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.event_handlers._session_start import AgentActivationResult
from gobby.hooks.event_handlers._session_start.context import classify_session_start_context
from gobby.hooks.event_handlers._session_start.flow import _log_session_start_timing
from gobby.hooks.event_handlers._session_start.materialize import session_start_should_defer
from gobby.hooks.event_handlers._session_start.terminal_runtime import (
    expire_stale_terminal_sessions_for_context,
    session_start_is_native_subagent_child,
)
from gobby.hooks.events import HookEventType
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._update_sentinel import UNSET
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("session_source", "existing_session", "terminal_context", "expected"),
    [
        ("startup", None, None, True),
        ("new", None, None, True),
        ("", None, None, True),
        (None, None, None, True),
        ("resume", None, None, False),
        ("startup", SimpleNamespace(status="active"), None, False),
        ("startup", SimpleNamespace(status="expired"), None, False),
        ("startup", None, {"gobby_acp_child": "1"}, False),
    ],
)
def test_session_start_should_defer(
    session_source: str | None,
    existing_session: object | None,
    terminal_context: dict[str, str] | None,
    expected: bool,
) -> None:
    event = make_event(
        HookEventType.SESSION_START,
        source="grok",
        data={"terminal_context": terminal_context} if terminal_context else {},
    )

    assert session_start_should_defer(event, existing_session, session_source) is expected


def test_session_start_should_not_defer_nested_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    event = make_event(
        HookEventType.SESSION_START,
        source="grok",
        data={"terminal_context": {"tmux_pane": "%42"}},
    )
    completed = SimpleNamespace(returncode=0, stdout="droid\n")
    monkeypatch.setattr(
        "gobby.hooks.event_handlers._session_start.terminal_runtime.subprocess.run",
        lambda *args, **kwargs: completed,
    )

    assert session_start_should_defer(event, None, "startup") is False


def test_session_start_is_native_subagent_child_requires_live_parent_with_subagent() -> None:
    parent = SimpleNamespace(id="parent-live", status="active", agent_run_id=None, agent_depth=0)
    session_manager = MagicMock()
    session_manager.find_live_interactive_pane_owner.return_value = parent
    session_manager.db.fetchone.return_value = {
        "variables": {"subagent_count": 1, "is_subagent": True}
    }
    terminal_context = {
        "tmux_pane": "%90",
        "tmux_socket_path": "/tmp/tmux-501/default",
    }

    assert (
        session_start_is_native_subagent_child(
            session_manager,
            terminal_context,
            "21000000-0000-4000-8000-000000000009",
        )
        is True
    )

    session_manager.db.fetchone.return_value = None
    assert (
        session_start_is_native_subagent_child(
            session_manager,
            terminal_context,
            "21000000-0000-4000-8000-000000000009",
        )
        is False
    )
    session_manager.find_live_interactive_pane_owner.return_value = None
    session_manager.db.fetchone.return_value = {
        "variables": {"subagent_count": 1, "is_subagent": True}
    }
    assert (
        session_start_is_native_subagent_child(
            session_manager,
            terminal_context,
            "21000000-0000-4000-8000-000000000009",
        )
        is False
    )


def test_session_start_skips_native_subagent_inheriting_tty(
    mock_dependencies: dict[str, Any],
    mock_empty_session_variable_manager: MagicMock,
) -> None:
    parent = SimpleNamespace(id="parent-live", status="active", agent_run_id=None, agent_depth=0)
    storage = mock_dependencies["session_storage"]
    storage.get.return_value = None
    storage.find_live_interactive_pane_owner.return_value = parent
    storage.db.fetchone.return_value = {"variables": {"subagent_count": 1, "is_subagent": True}}
    handlers = EventHandlers(**mock_dependencies)
    event = make_event(
        HookEventType.SESSION_START,
        session_id="01a04561-child",
        source="grok",
        data={
            "source": "startup",
            "cwd": "/work",
            "terminal_context": {
                "tmux_pane": "%90",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
        },
    )
    event.machine_id = "21000000-0000-4000-8000-000000000009"

    response = handlers.handle_session_start(event)

    assert response.decision == "allow"
    storage.register_session.assert_not_called()
    storage.register.assert_not_called()


def _agent_activation_context() -> AgentActivationResult:
    return AgentActivationResult(
        agent_name="default",
        description=None,
        rules_count=0,
        skills_count=0,
        variables_count=0,
        injected_skill_names=[],
    )


def _session_variable_handler(db: Any) -> MagicMock:
    handler = MagicMock()
    handler._session_manager = SimpleNamespace(db=db)
    handler.logger = MagicMock()
    return handler


def _register_context_claim_session(db: HubDatabase, *, external_id: str) -> str:
    project = LocalProjectManager(db).create(
        name=external_id,
        repo_path="/context-claim-tests",
    )
    session = SessionManager(db).register(
        external_id=external_id,
        machine_id=require_machine_id(),
        source="claude",
        project_id=project.id,
    )
    return session.id


def test_log_session_start_timing_tolerates_missing_total() -> None:
    debug_messages: list[str] = []
    info_messages: list[str] = []
    handler = SimpleNamespace(
        logger=SimpleNamespace(
            debug=debug_messages.append,
            info=info_messages.append,
        )
    )

    _log_session_start_timing(
        handler,
        session_source="codex",
        session_id="sess-1",
        timings={"resolve": 5},
    )

    assert debug_messages == ["SESSION_START timing [codex]: resolve=5ms"]
    assert info_messages == []


def test_expire_stale_terminal_sessions_for_reused_tmux_context() -> None:
    session_manager = MagicMock()
    session_manager.db.fetchall.return_value = [
        {
            "id": "stale-same-pane",
            "terminal_context": {
                "tmux_pane": "%73",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
        },
        {
            "id": "different-pane",
            "terminal_context": {
                "tmux_pane": "%74",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
        },
        {
            "id": "different-socket",
            "terminal_context": {
                "tmux_pane": "%73",
                "tmux_socket_path": "/tmp/tmux-501/other",
            },
        },
    ]
    session_manager.mark_session_expired.return_value = True
    handler = SimpleNamespace(_session_manager=session_manager, logger=MagicMock())

    expire_stale_terminal_sessions_for_context(
        handler,
        session_id="current-session",
        project_id="project-1",
        terminal_context={
            "tmux_pane": "%73",
            "tmux_socket_path": "/tmp/tmux-501/default",
        },
    )

    session_manager.db.fetchall.assert_called_once()
    assert session_manager.db.fetchall.call_args.args[1] == (
        "project-1",
        "terminal",
        "active",
        "paused",
        "current-session",
        200,
    )
    session_manager.mark_session_expired.assert_called_once_with(
        "stale-same-pane",
        cause="context_reuse",
    )


def test_expire_stale_terminal_sessions_fail_open_when_expiry_fails() -> None:
    session_manager = MagicMock()
    session_manager.db.fetchall.return_value = [
        {
            "id": "stale-same-pane",
            "terminal_context": {
                "tmux_pane": "%73",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
        },
        {
            "id": "stale-second",
            "terminal_context": {
                "tmux_pane": "%73",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
        },
    ]
    session_manager.mark_session_expired.side_effect = [RuntimeError("db busy"), True]
    handler = SimpleNamespace(_session_manager=session_manager, logger=MagicMock())

    expire_stale_terminal_sessions_for_context(
        handler,
        session_id="current-session",
        project_id="project-1",
        terminal_context={
            "tmux_pane": "%73",
            "tmux_socket_path": "/tmp/tmux-501/default",
        },
    )

    assert session_manager.mark_session_expired.call_args_list == [
        call("stale-same-pane", cause="context_reuse"),
        call("stale-second", cause="context_reuse"),
    ]
    handler.logger.warning.assert_called_once()


class TestSessionHandlers:
    """Test SESSION_START and SESSION_END handlers."""

    def test_session_start_allows(
        self,
        event_handlers: EventHandlers,
        mock_dependencies: dict[str, Any],
        mock_empty_session_variable_manager: MagicMock,
    ) -> None:
        """Test SESSION_START handler allows by default."""
        event = make_event(HookEventType.SESSION_START, session_id="ext-123")
        response = event_handlers.handle_session_start(event)
        assert response.decision == "allow"

    def test_session_end_allows(self, event_handlers: EventHandlers) -> None:
        """Test SESSION_END handler allows by default."""
        event = make_event(
            HookEventType.SESSION_END,
            metadata={"_platform_session_id": "plat-123"},
        )
        response = event_handlers.handle_session_end(event)
        assert response.decision == "allow"


class TestSessionStartContextClaim:
    """Test shared SessionStart startup context claim behavior."""

    def test_duplicate_session_start_claims_full_context_once(self, temp_db: Any) -> None:
        handler = _session_variable_handler(temp_db)
        session_id = _register_context_claim_session(
            temp_db,
            external_id="context-claim-sequential",
        )

        first = classify_session_start_context(
            handler,
            session_id=session_id,
            session=None,
            session_source="startup",
            is_existing_session=False,
        )
        second = classify_session_start_context(
            handler,
            session_id=session_id,
            session=None,
            session_source="startup",
            is_existing_session=True,
        )

        assert first.mode == "full"
        assert second.mode == "live"
        variables = SessionVariableManager(temp_db).get_variables(session_id)
        assert variables["_startup_context_injected"] is True

    def test_concurrent_duplicate_session_start_claims_full_context_once(
        self, temp_db: Any
    ) -> None:
        handler = _session_variable_handler(temp_db)
        session_id = _register_context_claim_session(
            temp_db,
            external_id="context-claim-concurrent",
        )
        barrier = Barrier(8)

        def classify_once(_: int) -> str:
            barrier.wait()
            return classify_session_start_context(
                handler,
                session_id=session_id,
                session=None,
                session_source="startup",
                is_existing_session=False,
            ).mode

        with ThreadPoolExecutor(max_workers=8) as executor:
            modes = list(executor.map(classify_once, range(8)))

        assert modes.count("full") == 1
        assert modes.count("live") == 7
        variables = SessionVariableManager(temp_db).get_variables(session_id)
        assert variables["_startup_context_injected"] is True

    def test_explicit_context_loss_bypasses_existing_startup_claim(self, temp_db: Any) -> None:
        handler = _session_variable_handler(temp_db)
        session_id = _register_context_claim_session(
            temp_db,
            external_id="context-claim-explicit-loss",
        )
        SessionVariableManager(temp_db).merge_variables(
            session_id,
            {"_startup_context_injected": True},
        )

        decision = classify_session_start_context(
            handler,
            session_id=session_id,
            session=None,
            session_source="clear",
            is_existing_session=True,
        )

        assert decision.mode == "full"
        assert decision.explicit_context_loss is True
        variables = SessionVariableManager(temp_db).get_variables(session_id)
        assert variables["_startup_context_injected"] is True


class TestSessionStartPreCreatedSession:
    """Test SESSION_START handling for pre-created sessions (terminal mode agents)."""

    def test_pre_created_session_found_and_updated(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test pre-created session is found and updated."""
        # Create a mock session object
        mock_session = MagicMock()
        mock_session.id = "sess-pre-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None

        # Configure session_storage.get to return the session
        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-pre-123",
            data={"transcript_path": "/path/to/transcript.jsonl", "cwd": "/some/dir"},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert response.metadata.get("is_pre_created") is True
        assert response.metadata.get("session_id") == "sess-pre-123"
        mock_dependencies["session_storage"].update.assert_called_once()

    def test_pre_created_session_backfills_terminal_context(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Pre-created sessions should persist terminal metadata from runtime hooks."""
        mock_session = MagicMock()
        mock_session.id = "sess-pre-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None
        mock_session.title = "Useful synthesized title"
        mock_session.handoff_markdown = None
        mock_session.terminal_context = None

        updated_session = MagicMock()
        updated_session.id = "sess-pre-123"
        updated_session.project_id = "proj-123"
        updated_session.parent_session_id = None
        updated_session.agent_depth = 0
        updated_session.agent_run_id = None
        updated_session.title = "Useful synthesized title"
        updated_session.handoff_markdown = None
        updated_session.terminal_context = {"tmux_pane": "%77", "parent_pid": 123}

        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_storage"].update.return_value = mock_session
        mock_dependencies["session_manager"].backfill_terminal_context.return_value = (
            updated_session,
            True,
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-pre-123",
            data={
                "transcript_path": "/path/to/transcript.jsonl",
                "terminal_context": {"tmux_pane": "%77", "parent_pid": 123},
            },
        )

        with (
            patch(
                "gobby.hooks.event_handlers._session_start.schedule_tmux_window_rename"
            ) as mock_schedule,
            patch(
                "gobby.hooks.event_handlers._session_start.terminal_runtime.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout=""),
            ),
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].backfill_terminal_context.assert_called_once_with(
            "sess-pre-123",
            {"tmux_pane": "%77", "parent_pid": 123},
        )
        mock_schedule.assert_called_once()
        assert response.metadata.get("terminal_tmux_pane") == "%77"

    def test_pre_created_session_renames_empty_title_with_cwd(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Pre-created SessionStart should rename panes using cwd fallback."""
        mock_session = MagicMock()
        mock_session.id = "sess-pre-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None
        mock_session.title = None
        mock_session.handoff_markdown = None
        mock_session.terminal_context = None

        updated_session = MagicMock()
        updated_session.id = "sess-pre-123"
        updated_session.project_id = "proj-123"
        updated_session.parent_session_id = None
        updated_session.agent_depth = 0
        updated_session.agent_run_id = None
        updated_session.title = None
        updated_session.handoff_markdown = None
        updated_session.terminal_context = {
            "tmux_pane": "%77",
            "parent_pid": 123,
            "cwd": "/work/repos/gobby",
        }

        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_storage"].update.return_value = mock_session
        mock_dependencies["session_manager"].backfill_terminal_context.return_value = (
            updated_session,
            True,
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-pre-123",
            data={
                "cwd": "/work/repos/gobby",
                "terminal_context": {"tmux_pane": "%77", "parent_pid": 123},
            },
        )

        with (
            patch(
                "gobby.hooks.event_handlers._session_start.schedule_tmux_window_rename"
            ) as mock_schedule,
            patch(
                "gobby.hooks.event_handlers._session_start.terminal_runtime.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout=""),
            ),
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert response.metadata.get("is_pre_created") is True
        assert response.metadata.get("session_id") == "sess-pre-123"
        assert response.metadata.get("terminal_tmux_pane") == "%77"
        mock_dependencies["session_manager"].backfill_terminal_context.assert_called_once_with(
            "sess-pre-123",
            {"tmux_pane": "%77", "parent_pid": 123, "cwd": "/work/repos/gobby"},
        )
        mock_schedule.assert_called_once_with(
            updated_session,
            "",
            loop=mock_dependencies["session_coordinator"]._event_loop,
        )

    def test_pre_created_session_with_parent(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test pre-created session with parent session ID includes parent context."""
        mock_session = MagicMock()
        mock_session.id = "sess-child-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = "sess-parent-456"
        mock_session.agent_depth = 1
        mock_session.agent_run_id = None

        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-child-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        # Parent session info in context and metadata
        assert "Parent session: sess-parent-456" in response.context
        assert response.metadata["parent_session_id"] == "sess-parent-456"
        assert response.metadata.get("is_pre_created") is True

    def test_pre_created_session_with_agent_run_id(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test pre-created session with agent_run_id starts the agent run."""
        mock_session = MagicMock()
        mock_session.id = "sess-agent-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = "run-456"

        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-agent-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_dependencies["session_coordinator"].start_agent_run.assert_called_once_with("run-456")

    def test_pre_created_session_agent_run_start_error(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test error starting agent run is handled gracefully."""
        mock_session = MagicMock()
        mock_session.id = "sess-agent-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = "run-456"

        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_coordinator"].start_agent_run.side_effect = Exception(
            "Failed to start"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-agent-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_pre_created_session_registers_with_message_processor(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test pre-created session registers with message processor."""
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None

        mock_dependencies["session_storage"].get.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        handlers.handle_session_start(event)

        mock_dependencies["message_processor_resolver"]().register_session.assert_called_once_with(
            "sess-123", "/path/to/transcript.jsonl", source="claude"
        )
        assert (
            handlers._session_message_processors["sess-123"]
            is mock_dependencies["message_processor_resolver"]()
        )
        assert mock_dependencies["message_processor_resolver"]().register_session.call_count == 1
        assert (
            mock_dependencies["message_processor_resolver"]().register_session.call_args is not None
        )

    def test_pre_created_session_message_processor_error(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test error registering with message processor is handled gracefully."""
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None

        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["message_processor_resolver"]().register_session.side_effect = Exception(
            "Registration failed"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"

    def test_existing_web_chat_session_found_by_external_id(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Codex thread-start events should reuse the durable web-chat row."""
        mock_session = MagicMock()
        mock_session.id = "sess-web-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None
        mock_session.workflow_name = None
        mock_session.session_type = "web_chat"
        mock_session.terminal_context = {}

        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_manager"].find_by_external_id.return_value = mock_session
        mock_dependencies["session_manager"].update.return_value = mock_session
        mock_dependencies["session_manager"].backfill_terminal_context.return_value = (
            mock_session,
            False,
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="codex-thread-123",
            data={"transcript_path": "/path/to/transcript.jsonl", "cwd": "/some/dir"},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert response.metadata.get("is_pre_created") is True
        assert event.metadata["_platform_session_id"] == "sess-web-123"
        mock_dependencies["session_manager"].register_session.assert_not_called()
        mock_dependencies["message_processor_resolver"]().register_session.assert_called_once_with(
            "sess-web-123",
            "/path/to/transcript.jsonl",
            source="claude",
        )

    def test_pre_created_session_coordinator_error(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test error registering session with coordinator is handled."""
        mock_session = MagicMock()
        mock_session.id = "sess-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None

        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_coordinator"].register_session.side_effect = Exception(
            "Coordinator error"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-123",
            data={"transcript_path": "/path/to/transcript.jsonl"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_live_pre_created_session_omits_startup_persona(
        self,
        mock_svm_cls: MagicMock,
        mock_dependencies: dict[str, Any],
    ) -> None:
        """Resumed sessions with prior context get live context, not startup persona."""
        mock_session = MagicMock()
        mock_session.id = "sess-pre-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None
        mock_session.workflow_name = None
        mock_session.terminal_context = None
        mock_session.context_injected = True
        mock_session.message_count = 4
        mock_session.turn_count = 2
        mock_session.seq_num = 6273

        task = MagicMock()
        task.seq_num = 15237
        task.status = "in_progress"
        task.title = "Fix prompt-boundary replays"
        task.claimed_by_session_id = "sess-pre-123"

        mock_svm = MagicMock()
        mock_svm.get_variables.return_value = {
            "_startup_context_injected": True,
            "task_claimed": True,
            "claimed_tasks": {"task-uuid": "#15237"},
        }
        mock_svm_cls.return_value = mock_svm
        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_manager"].update.return_value = mock_session
        mock_dependencies["task_manager"].get_task.return_value = task

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-pre-123",
            data={"source": "resume", "transcript_path": "/path/to/transcript.jsonl"},
        )

        with (
            patch.object(
                handlers, "_activate_default_agent", return_value=_agent_activation_context()
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_handoff_compact_continuation",
                return_value=False,
            ),
        ):
            response = handlers.handle_session_start(event)

        assert response.system_message == "\nGobby Session ID: #6273 (sess-pre-123)"
        assert response.context is not None
        assert "Claimed task refs: #15237" in response.context
        assert "## Role" not in response.context
        assert "## Personality" not in response.context
        assert "## Instructions" not in response.context
        assert "## Claimed Tasks (Persisted)" not in response.context
        mock_dependencies["session_manager"].update_terminal_pickup_metadata.assert_not_called()

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_first_pre_created_pickup_defers_startup_persona_to_first_prompt(
        self,
        mock_svm_cls: MagicMock,
        mock_dependencies: dict[str, Any],
    ) -> None:
        """Pre-created sessions without prior context evidence reset first-prompt injection."""
        mock_session = MagicMock()
        mock_session.id = "sess-first-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None
        mock_session.workflow_name = None
        mock_session.terminal_context = None
        mock_session.context_injected = False
        mock_session.message_count = 0
        mock_session.turn_count = 0
        mock_session.seq_num = 6274

        mock_svm = MagicMock()
        mock_svm.get_variables.return_value = {}
        mock_svm.claim_startup_context.return_value = "full"
        mock_svm_cls.return_value = mock_svm
        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_manager"].update.return_value = mock_session

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="sess-first-123",
            data={"source": "resume", "transcript_path": "/path/to/transcript.jsonl"},
        )

        with (
            patch.object(
                handlers, "_activate_default_agent", return_value=_agent_activation_context()
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_handoff_compact_continuation",
                return_value=False,
            ),
        ):
            response = handlers.handle_session_start(event)

        context = response.context or ""
        assert "## Role" not in context
        assert "## Personality" not in context
        assert "## Instructions" not in context
        assert (
            call(
                "sess-first-123",
                {
                    "_agent_context_injected": False,
                    "_agent_context_rehydrate_pending": True,
                    "wiki_overview_injected": False,
                },
            )
            in mock_svm.merge_variables.call_args_list
        )
        mock_dependencies[
            "session_manager"
        ].update_terminal_pickup_metadata.assert_called_once_with(
            "sess-first-123",
            context_injected=True,
        )
        mock_svm.claim_startup_context.assert_called_once_with("sess-first-123")

    @pytest.mark.parametrize(
        ("source", "pending_reset"),
        [("clear", False), ("compact", False), ("resume", True)],
    )
    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_context_loss_sources_reset_first_prompt_agent_context(
        self,
        mock_svm_cls: MagicMock,
        source: str,
        pending_reset: bool,
        mock_dependencies: dict[str, Any],
    ) -> None:
        """Explicit context-loss starts reset first-prompt injection."""
        mock_session = MagicMock()
        mock_session.id = f"sess-{source}-123"
        mock_session.project_id = "proj-123"
        mock_session.parent_session_id = None
        mock_session.agent_depth = 0
        mock_session.agent_run_id = None
        mock_session.workflow_name = None
        mock_session.terminal_context = None
        mock_session.context_injected = True
        mock_session.message_count = 8
        mock_session.turn_count = 3
        mock_session.seq_num = 7000

        mock_svm = MagicMock()
        mock_svm.get_variables.return_value = {
            "_startup_context_injected": True,
            "pending_context_reset": pending_reset,
        }
        mock_svm_cls.return_value = mock_svm
        mock_dependencies["session_storage"].get.return_value = mock_session
        mock_dependencies["session_manager"].update.return_value = mock_session
        # A clear start never reuses the predecessor row; it registers a successor.
        successor_id = f"sess-{source}-successor"
        mock_dependencies["session_manager"].register_session.return_value = successor_id
        expected_session_id = successor_id if source == "clear" else mock_session.id

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=mock_session.id,
            data={"source": source, "transcript_path": "/path/to/transcript.jsonl"},
        )

        with (
            patch.object(
                handlers, "_activate_default_agent", return_value=_agent_activation_context()
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_handoff_compact_continuation",
                return_value=False,
            ),
        ):
            response = handlers.handle_session_start(event)

        context = response.context or ""
        assert "## Role" not in context
        assert "## Personality" not in context
        assert "## Instructions" not in context
        assert (
            call(
                expected_session_id,
                {
                    "_agent_context_injected": False,
                    "_agent_context_rehydrate_pending": True,
                    "wiki_overview_injected": False,
                },
            )
            in mock_svm.merge_variables.call_args_list
        )


class TestSessionStartNewSession:
    """Test SESSION_START handling for new sessions."""

    def test_explicit_resume_rebinds_resolved_terminal_row(
        self,
        mock_dependencies: dict[str, Any],
        mock_empty_session_variable_manager: MagicMock,
    ) -> None:
        fresh_context = {
            "tmux_pane": "%88",
            "tmux_window_id": "@12",
            "tmux_session": "work",
            "tmux_socket_path": "/private/tmp/tmux-501/new",
            "pid": 4242,
            "tty": "/dev/ttys012",
        }
        persisted = SimpleNamespace(
            id="platform-session-id",
            external_id="provider-session-id",
            machine_id="21000000-0000-4000-8000-000000000008",
            project_id="proj-123",
            session_type="terminal",
            status="expired",
            source="codex",
            terminal_context={"tmux_pane": "%10"},
            parent_session_id=None,
            agent_depth=0,
            agent_run_id=None,
            title="Resumed work",
            workflow_name=None,
            message_count=8,
            turn_count=3,
            seq_num=42,
        )
        resumed = SimpleNamespace(**vars(persisted))
        resumed.status = "active"
        resumed.terminal_context = fresh_context
        storage = mock_dependencies["session_storage"]
        storage.find_by_external_id.return_value = persisted
        storage.rebind_resumed_terminal_session.return_value = resumed
        storage.get.side_effect = lambda session_id: (
            resumed if session_id == persisted.id else None
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=persisted.external_id,
            source="codex",
            data={
                "source": "resume",
                "cwd": "/work/gobby",
                "transcript_path": "/tmp/resumed.jsonl",
                "terminal_context": fresh_context,
            },
            metadata={},
        )
        event.machine_id = persisted.machine_id

        compact_resolution = SimpleNamespace(
            ambiguous=False,
            session=None,
            conflicting_session_ids=(),
        )
        project_resolution = SimpleNamespace(
            skipped=False,
            reason="matched test project",
            project_id="proj-123",
        )
        with (
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_hook_project_context",
                return_value=project_resolution,
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.handoff.resolve_compact_continuation",
                return_value=compact_resolution,
            ),
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch("gobby.hooks.event_handlers._session_start.schedule_tmux_window_rename"),
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        storage.rebind_resumed_terminal_session.assert_called_once_with(
            persisted.id,
            machine_id=persisted.machine_id,
            project_id="proj-123",
            source="codex",
            transcript_path="/tmp/resumed.jsonl",
            terminal_context={**fresh_context, "cwd": "/work/gobby"},
            workflow_name=None,
            agent_depth=0,
            sandbox_enabled=None,
        )
        mock_dependencies["session_manager"].register_session.assert_not_called()
        assert event.metadata["_platform_session_id"] == persisted.id
        assert event.session_id == persisted.external_id

    def test_explicit_resume_without_stored_session_registers_new_session(
        self,
        mock_dependencies: dict[str, Any],
        mock_empty_session_variable_manager: MagicMock,
    ) -> None:
        storage = mock_dependencies["session_storage"]
        storage.get.return_value = None
        storage.find_by_external_id.return_value = None
        mock_dependencies["session_manager"].register_session.return_value = "new-resume-id"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="missing-resume-provider-id",
            source="codex",
            data={"source": "resume", "cwd": "/work/gobby"},
            metadata={},
        )
        event.machine_id = "21000000-0000-4000-8000-000000000008"
        project_resolution = SimpleNamespace(
            skipped=False,
            reason="matched test project",
            project_id="proj-123",
        )

        with (
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_hook_project_context",
                return_value=project_resolution,
            ),
            patch.object(handlers, "_activate_default_agent", return_value=None),
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].register_session.assert_called_once()
        assert event.metadata["_platform_session_id"] == "new-resume-id"
        storage.rebind_resumed_terminal_session.assert_not_called()

    def test_ordinary_delayed_start_cannot_reactivate_expired_precreated_session(
        self,
        mock_dependencies: dict[str, Any],
        mock_empty_session_variable_manager: MagicMock,
    ) -> None:
        expired = SimpleNamespace(id="expired-platform-id", status="expired")
        storage = mock_dependencies["session_storage"]
        storage.get.return_value = expired
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=expired.id,
            source="codex",
            data={"source": "startup"},
        )

        with patch.object(handlers, "_handle_pre_created_session") as precreated:
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        precreated.assert_not_called()
        storage.update.assert_not_called()
        storage.register_session.assert_not_called()
        storage.rebind_resumed_terminal_session.assert_not_called()

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_clear_session_starts_without_handoff(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict[str, Any]
    ) -> None:
        """A native clear start creates an independent session."""
        mock_sv_mgr = MagicMock(get_variables=MagicMock(return_value={}))
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        mock_parent = MagicMock()
        mock_parent.id = "parent-sess-123"
        mock_parent.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}

        # No pre-created session found
        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={
                "source": "clear",
                "cwd": "/some/dir",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
            metadata={},
        )
        event.machine_id = "21000000-0000-4000-8000-000000000008"

        with patch.object(handlers, "_activate_default_agent", return_value=None):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert "_parent_session_id" not in event.metadata
        mock_dependencies["session_storage"].find_parent.assert_not_called()
        register_kwargs = mock_dependencies["session_manager"].register_session.call_args.kwargs
        assert register_kwargs["parent_session_id"] is UNSET
        mock_dependencies["session_manager"].mark_session_expired.assert_not_called()
        mock_dependencies["task_manager"].claim_task.assert_not_called()
        copied_keys = {
            "session_summary",
            "full_session_summary",
            "handoff_summary_injectable",
            "task_claimed",
            "claimed_tasks",
        }
        for args, _kwargs in mock_sv_mgr.merge_variables.call_args_list:
            assert copied_keys.isdisjoint(args[1])

    def test_startup_session_does_not_adopt_stale_parent(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test that fresh startup sessions never search for handoff parents."""
        mock_parent = MagicMock()
        mock_parent.id = "stale-parent-123"

        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-789"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-456",
            data={"source": "startup", "cwd": "/some/dir"},
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        # find_parent should NOT be called for startup sessions
        mock_dependencies["session_storage"].find_parent.assert_not_called()
        mock_dependencies["session_manager"].register_session.assert_not_called()
        assert "_platform_session_id" not in event.metadata

    def test_nested_grok_session_in_droid_pane_is_not_registered(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        mock_dependencies["session_storage"].get.return_value = None
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="grok-child-session",
            source="grok",
            data={
                "source": "startup",
                "cwd": "/some/dir",
                "terminal_context": {
                    "tmux_pane": "%75",
                    "tmux_socket_path": "/private/tmp/tmux-501/default",
                },
            },
            metadata={},
        )

        with patch(
            "gobby.hooks.event_handlers._session_start.terminal_runtime.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="droid\n"),
        ) as mock_run:
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_run.assert_called_once()
        mock_dependencies["session_manager"].register_session.assert_not_called()
        mock_dependencies["session_storage"].find_parent.assert_not_called()

    def test_blank_external_id_returns_allow_without_registration(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Malformed SessionStart hooks without an external id are ignored."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="  ",
            data={
                "source": "startup",
                "cwd": "/some/dir",
                "transcript_path": "/path/to/transcript.jsonl",
            },
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_dependencies["session_storage"].get.assert_not_called()
        mock_dependencies["session_storage"].find_parent.assert_not_called()
        mock_dependencies["session_manager"].register_session.assert_not_called()
        mock_dependencies["session_coordinator"].register_session.assert_not_called()
        mock_dependencies["message_processor_resolver"]().register_session.assert_not_called()

    def test_handoff_db_error_still_returns_session_banner(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """A handoff-variable DB error must not abort session-start injection."""
        new_session = MagicMock()
        new_session.id = "new-sess-db-error"
        new_session.seq_num = 77
        new_session.project_id = "proj-123"
        new_session.parent_session_id = "parent-sess-123"
        new_session.agent_depth = 0
        new_session.agent_run_id = None
        new_session.title = None
        new_session.terminal_context = {}

        mock_parent = MagicMock()
        mock_parent.id = "parent-sess-123"
        mock_parent.terminal_context = {}

        mock_dependencies["session_storage"].get.side_effect = [None, new_session, new_session]
        mock_dependencies["session_storage"].find_parent.return_value = mock_parent
        mock_dependencies["session_manager"].register_session.return_value = new_session.id
        mock_dependencies["task_manager"].list_tasks.return_value = []

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-db-error",
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "skip_default_agent_activation": True,
            },
            metadata={},
        )

        with (
            patch(
                "gobby.hooks.event_handlers._session_start.materialize.seed_user_profile_content"
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.materialize.prepare_compact_continuation_variables",
                side_effect=psycopg.OperationalError("handoff vars unavailable"),
            ),
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert response.system_message is None
        assert "_platform_session_id" not in event.metadata
        mock_dependencies["session_manager"].register_session.assert_not_called()

    def test_new_session_start_renames_captured_tmux_pane(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """New SessionStart should persist cwd and rename captured panes immediately."""
        new_session = MagicMock()
        new_session.id = "new-sess-456"
        new_session.project_id = "proj-123"
        new_session.parent_session_id = None
        new_session.agent_depth = 0
        new_session.agent_run_id = None
        new_session.title = None
        new_session.terminal_context = {"tmux_pane": "%88", "cwd": "/work/repos/gobby"}

        mock_dependencies["session_storage"].get.side_effect = [None, new_session]
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-rename",
            data={
                "cwd": "/work/repos/gobby",
                "terminal_context": {"tmux_pane": "%88"},
            },
            metadata={},
        )

        with patch(
            "gobby.hooks.event_handlers._session_start.schedule_tmux_window_rename"
        ) as mock_schedule:
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].register_session.assert_not_called()
        mock_schedule.assert_not_called()

    def test_new_session_parent_lookup_error(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test error looking up parent session is handled gracefully."""
        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_storage"].find_parent.side_effect = Exception("Lookup error")
        mock_dependencies["session_manager"].register_session.return_value = "new-sess-456"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={"source": "compact", "cwd": "/some/dir"},
        )

        response = handlers.handle_session_start(event)

        # Should still allow despite error
        assert response.decision == "allow"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_missing_compact_session_degrades_to_start_without_expiry(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict[str, Any]
    ) -> None:
        """A missing compact row degrades to startup without expiring another session."""
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))

        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_manager"].register_session.return_value = ""

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={
                "source": "compact",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
        )

        with patch.object(handlers, "_activate_default_agent") as activate_agent:
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert "_platform_session_id" not in event.metadata
        mock_dependencies["session_manager"].mark_session_expired.assert_not_called()
        activate_agent.assert_not_called()

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_compact_terminal_identity_conflict_blocks_before_registration(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict[str, Any]
    ) -> None:
        """A contradicting terminal identity blocks with no session writes."""
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))

        row = MagicMock()
        row.id = "sess-123"
        row.status = "handoff_ready"
        row.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}

        mock_dependencies["session_storage"].get.return_value = None
        mock_dependencies["session_storage"].find_by_external_id.return_value = row

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={
                "source": "compact",
                "terminal_context": {"tmux_pane": "%99", "tmux_socket_path": "/tmp/tmux"},
            },
        )

        with patch.object(handlers, "_activate_default_agent") as activate_agent:
            response = handlers.handle_session_start(event)

        assert response.decision == "block"
        assert "_platform_session_id" not in event.metadata
        mock_dependencies["session_manager"].register_session.assert_not_called()
        mock_dependencies["session_manager"].mark_session_expired.assert_not_called()
        activate_agent.assert_not_called()

    def test_materialized_session_coordinator_registration_error(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test error registering session with coordinator is handled."""
        mock_dependencies["session_coordinator"].register_session.side_effect = Exception(
            "Coordinator error"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            session_id="ext-123",
            data={
                "transcript_path": "/path/to/transcript.jsonl",
                "skip_default_agent_activation": True,
            },
        )
        session = SimpleNamespace(
            id="new-sess-456",
            project_id="proj-123",
            parent_session_id=None,
            transcript_path="/path/to/transcript.jsonl",
            terminal_context={},
            title=None,
        )

        additional_context = handlers._activate_materialized_session(
            event,
            session.id,
            session_obj=session,
            project_id=session.project_id,
            transcript_path=session.transcript_path,
        )

        assert additional_context == []

    def test_materialized_session_message_processor_registration(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test new session registers with message processor."""
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            session_id="ext-123",
            data={
                "transcript_path": "/path/to/transcript.jsonl",
                "skip_default_agent_activation": True,
            },
        )
        session = SimpleNamespace(
            id="new-sess-456",
            project_id="proj-123",
            parent_session_id=None,
            transcript_path="/path/to/transcript.jsonl",
            terminal_context={},
            title=None,
        )

        handlers._activate_materialized_session(
            event,
            session.id,
            session_obj=session,
            project_id=session.project_id,
            transcript_path=session.transcript_path,
        )

        mock_dependencies["message_processor_resolver"]().register_session.assert_called_once_with(
            "new-sess-456", "/path/to/transcript.jsonl", source="claude"
        )
        assert (
            handlers._session_message_processors["new-sess-456"]
            is mock_dependencies["message_processor_resolver"]()
        )
        assert mock_dependencies["message_processor_resolver"]().register_session.call_count == 1
        assert (
            mock_dependencies["message_processor_resolver"]().register_session.call_args is not None
        )

    def test_session_start_resolves_rebuilt_processor_and_disabled_tracking_noops(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        old_processor = MagicMock()
        rebuilt_processor = MagicMock()
        current: list[Any | None] = [old_processor]
        mock_dependencies["message_processor_resolver"] = lambda: current[0]
        handlers = EventHandlers(**mock_dependencies)

        current[0] = rebuilt_processor
        first_event = make_event(
            HookEventType.BEFORE_AGENT,
            session_id="ext-1",
            data={
                "transcript_path": "/path/to/first.jsonl",
                "skip_default_agent_activation": True,
            },
        )
        first_session = SimpleNamespace(
            id="new-sess-1",
            project_id="proj-123",
            parent_session_id=None,
            transcript_path="/path/to/first.jsonl",
            terminal_context={},
            title=None,
        )
        handlers._activate_materialized_session(
            first_event,
            first_session.id,
            session_obj=first_session,
            project_id=first_session.project_id,
            transcript_path=first_session.transcript_path,
        )
        rebuilt_processor.register_session.assert_called_once_with(
            "new-sess-1", "/path/to/first.jsonl", source="claude"
        )
        old_processor.register_session.assert_not_called()

        current[0] = None
        second_event = make_event(
            HookEventType.BEFORE_AGENT,
            session_id="ext-2",
            data={
                "transcript_path": "/path/to/second.jsonl",
                "skip_default_agent_activation": True,
            },
        )
        second_session = SimpleNamespace(
            id="new-sess-2",
            project_id="proj-123",
            parent_session_id=None,
            transcript_path="/path/to/second.jsonl",
            terminal_context={},
            title=None,
        )
        handlers._activate_materialized_session(
            second_event,
            second_session.id,
            session_obj=second_session,
            project_id=second_session.project_id,
            transcript_path=second_session.transcript_path,
        )
        assert rebuilt_processor.register_session.call_count == 1

    def test_materialized_session_message_processor_error(
        self, mock_dependencies: dict[str, Any], mock_empty_session_variable_manager: MagicMock
    ) -> None:
        """Test error registering with message processor is handled."""
        mock_dependencies["message_processor_resolver"]().register_session.side_effect = Exception(
            "Registration failed"
        )

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            session_id="ext-123",
            data={
                "transcript_path": "/path/to/transcript.jsonl",
                "skip_default_agent_activation": True,
            },
        )
        session = SimpleNamespace(
            id="new-sess-456",
            project_id="proj-123",
            parent_session_id=None,
            transcript_path="/path/to/transcript.jsonl",
            terminal_context={},
            title=None,
        )

        handlers._activate_materialized_session(
            event,
            session.id,
            session_obj=session,
            project_id=session.project_id,
            transcript_path=session.transcript_path,
        )

        assert "new-sess-456" not in handlers._session_message_processors

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_new_session_with_task_id_context(
        self, mock_sv_mgr_cls: MagicMock, mock_dependencies: dict[str, Any]
    ) -> None:
        """Test new session includes task context when task_id present."""
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.return_value = {}
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            session_id="ext-123",
            data={"skip_default_agent_activation": True},
        )
        event.task_id = "task-789"
        event.metadata["_task_title"] = "Implement feature X"
        session = SimpleNamespace(
            id="new-sess-456",
            project_id="proj-123",
            parent_session_id=None,
            transcript_path=None,
            terminal_context={},
            title=None,
        )

        additional_context = handlers._activate_materialized_session(
            event,
            session.id,
            session_obj=session,
            project_id=session.project_id,
        )
        context = "\n".join(additional_context)

        assert "Active Task Context" in context
        assert "task-789" in context
        assert "Implement feature X" in context


def test_resolve_agent_name_reads_config_without_resolving_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.hooks.event_handlers._session_start.agents import resolve_agent_name

    handler = SimpleNamespace(_session_manager=SimpleNamespace(db=MagicMock()))
    variables = MagicMock()
    variables.get_variables.return_value = {}
    monkeypatch.setattr(
        "gobby.workflows.state_manager.SessionVariableManager",
        MagicMock(return_value=variables),
    )
    repository = MagicMock()
    repository.read.return_value = SimpleNamespace(values={"default_agent": "gobby"})
    monkeypatch.setattr(
        "gobby.storage.config_repository.ConfigRepository",
        MagicMock(return_value=repository),
    )

    assert resolve_agent_name(handler, "session-1", None) == "gobby"
    assert repository.read.call_count == 1
    assert repository.read.call_args == call(resolve_secrets=False)
    assert variables.get_variables.return_value == {}
