"""Handler execution, return value, and dependency isolation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.session_materialize import activate_deferred_session

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


class TestErrorIsolation:
    """Test handler error isolation."""

    def test_workflow_error_handled(
        self, event_handlers: EventHandlers, mock_dependencies: dict[str, Any]
    ) -> None:
        """Test workflow errors are handled gracefully."""
        mock_dependencies["workflow_handler"].evaluate.side_effect = Exception("Workflow error")
        event = make_event(HookEventType.BEFORE_AGENT, data={"prompt": "Hello"})
        response = event_handlers.handle_before_agent(event)
        assert response.decision in ("allow", "block")

    def test_missing_metadata_handled(self, event_handlers: EventHandlers) -> None:
        """Test missing metadata is handled gracefully."""
        event = make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})
        response = event_handlers.handle_before_tool(event)
        assert response.decision in ("allow", "block")


class TestReturnValues:
    """Test handler return values."""

    def test_returns_hook_response(self, event_handlers: EventHandlers) -> None:
        """Test handlers return HookResponse."""
        event = make_event(HookEventType.BEFORE_AGENT, data={"prompt": "Hello"})
        response = event_handlers.handle_before_agent(event)
        assert isinstance(response, HookResponse)
        assert hasattr(response, "decision")
        assert hasattr(response, "context")

    def test_session_banner_in_system_message_not_context(
        self,
        event_handlers: EventHandlers,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lean first-activity startup carries the banner in system_message; context stays empty."""
        manager = MagicMock()
        manager.get_variables.return_value = {}
        manager.merge_variables.return_value = True
        manager.claim_startup_context.return_value = SimpleNamespace(mode="full")
        monkeypatch.setattr(
            "gobby.workflows.state_manager.SessionVariableManager",
            MagicMock(return_value=manager),
        )
        session_obj = MagicMock()
        session_obj.parent_session_id = None
        session_obj.project_id = None
        session_obj.transcript_path = None
        session_obj.status = "active"

        def get_session(session_id: str) -> MagicMock | None:
            return session_obj if session_id == "sess-1" else None

        session_manager = cast(Any, event_handlers._session_manager)
        session_manager.get.side_effect = get_session
        hook_manager = MagicMock()
        hook_manager._event_handlers = event_handlers
        hook_manager._session_manager = session_manager
        hook_manager._evaluate_workflow_rules.return_value = (None, None)
        hook_manager._evaluate_blocking_webhooks.return_value = None
        hook_manager.get_machine_id.return_value = "machine-1"
        event = make_event(
            HookEventType.BEFORE_AGENT,
            data={"prompt": "Hello"},
            metadata={"_platform_session_id": "sess-1"},
        )

        assert (
            activate_deferred_session(
                hook_manager,
                event,
                BlockingEffectDeadline(123.0),
            )
            is None
        )

        assert event.metadata["_startup_context"] is None
        system_message = event.metadata["_startup_system_message"]
        assert system_message is not None
        assert "Gobby Session ID" in system_message


class TestNoManagerDependencies:
    """Test handlers when dependencies are None."""

    def test_session_start_no_dependencies(self) -> None:
        """Test SESSION_START works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.SESSION_START)

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"

    def test_session_end_no_dependencies(self) -> None:
        """Test SESSION_END works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.SESSION_END)

        response = handlers.handle_session_end(event)

        assert response.decision == "allow"

    def test_before_agent_no_dependencies(self) -> None:
        """Test BEFORE_AGENT works without dependencies."""
        handlers = EventHandlers()
        event = make_event(
            HookEventType.BEFORE_AGENT,
            data={"prompt": "Hello"},
        )

        response = handlers.handle_before_agent(event)

        assert response.decision == "allow"

    def test_after_agent_no_dependencies(self) -> None:
        """Test AFTER_AGENT works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.AFTER_AGENT)

        response = handlers.handle_after_agent(event)

        assert response.decision == "allow"

    def test_before_tool_no_dependencies(self) -> None:
        """Test BEFORE_TOOL works without dependencies."""
        handlers = EventHandlers()
        event = make_event(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Read"},
        )

        response = handlers.handle_before_tool(event)

        assert response.decision == "allow"

    def test_after_tool_no_dependencies(self) -> None:
        """Test AFTER_TOOL works without dependencies."""
        handlers = EventHandlers()
        event = make_event(
            HookEventType.AFTER_TOOL,
            data={"tool_name": "Read"},
        )

        response = handlers.handle_after_tool(event)

        assert response.decision == "allow"

    def test_pre_compact_no_dependencies(self) -> None:
        """Test PRE_COMPACT works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.PRE_COMPACT)

        response = handlers.handle_pre_compact(event)

        assert response.decision == "allow"

    def test_stop_no_dependencies(self) -> None:
        """Test STOP works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.STOP)

        response = handlers.handle_stop(event)

        assert response.decision == "allow"

    def test_notification_no_dependencies(self) -> None:
        """Test NOTIFICATION works without dependencies."""
        handlers = EventHandlers()
        event = make_event(HookEventType.NOTIFICATION)

        response = handlers.handle_notification(event)

        assert response.decision == "allow"


class TestApplyDebugEcho:
    """Tests for _apply_debug_echo helper on EventHandlersBase."""

    def test_debug_echo_from_workflow_config(self) -> None:
        """Test debug echo enabled via WorkflowConfig.debug_echo_context."""
        mock_config = MagicMock()
        mock_config.debug_echo_context = True

        handlers = EventHandlers(workflow_config=mock_config)
        response = HookResponse(decision="allow", context="some context")

        handlers._apply_debug_echo(response)

        assert response.system_message is not None
        assert "[DEBUG additionalContext]" in response.system_message
        assert "some context" in response.system_message

    def test_debug_echo_disabled(self) -> None:
        """Test no echo when debug_echo_context is False."""
        mock_config = MagicMock()
        mock_config.debug_echo_context = False

        handlers = EventHandlers(workflow_config=mock_config)
        response = HookResponse(decision="allow", context="some context")

        handlers._apply_debug_echo(response)

        assert response.system_message is None

    def test_debug_echo_resolves_current_workflow_config(self) -> None:
        disabled = MagicMock(debug_echo_context=False)
        enabled = MagicMock(debug_echo_context=True)
        current = [disabled]
        handlers = EventHandlers(workflow_config_resolver=lambda: current[0])

        first = HookResponse(decision="allow", context="first")
        handlers._apply_debug_echo(first)
        assert first.system_message is None

        current[0] = enabled
        second = HookResponse(decision="allow", context="second")
        handlers._apply_debug_echo(second)
        assert second.system_message is not None
        assert "second" in second.system_message

    def test_debug_echo_empty_context(self) -> None:
        """Test no echo when context is empty."""
        mock_config = MagicMock()
        mock_config.debug_echo_context = True

        handlers = EventHandlers(workflow_config=mock_config)
        response = HookResponse(decision="allow", context=None)

        handlers._apply_debug_echo(response)

        assert response.system_message is None

    def test_debug_echo_appends_to_existing_system_message(self) -> None:
        """Test echo appends to existing system_message rather than replacing."""
        mock_config = MagicMock()
        mock_config.debug_echo_context = True

        handlers = EventHandlers(workflow_config=mock_config)
        response = HookResponse(
            decision="allow",
            context="new context",
            system_message="Existing message",
        )

        handlers._apply_debug_echo(response)

        assert response.system_message is not None
        assert response.system_message.startswith("Existing message")
        assert "[DEBUG additionalContext]" in response.system_message
        assert "new context" in response.system_message

    def test_debug_echo_exists_on_base(self) -> None:
        """_apply_debug_echo is defined on EventHandlersBase."""
        from gobby.hooks.event_handlers._base import EventHandlersBase

        assert hasattr(EventHandlersBase, "_apply_debug_echo")
        assert callable(EventHandlersBase._apply_debug_echo)


LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


def _agy_transcript(tmp_path: Path) -> Path:
    return (
        tmp_path
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / "conv-1"
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )


def _pending_recheck_manager(
    manager: HookManager,
    *,
    session: SimpleNamespace,
) -> HookManager:
    mocks = cast(Any, manager)
    mocks._session_manager.get.return_value = session
    mocks._record_machine_ingress = MagicMock()
    mocks._record_session_activity_pulse = MagicMock()
    mocks._evaluate_workflow_rules = MagicMock(return_value=(None, None))
    mocks._evaluate_blocking_webhooks = MagicMock(return_value=None)
    mocks._event_handlers.get_handler.return_value = lambda _event: HookResponse(decision="allow")

    def resolve(event: HookEvent, *, apply_session_mutations: bool = True) -> str:
        del apply_session_mutations
        event.metadata["_platform_session_id"] = session.id
        return str(session.id)

    mocks._session_lookup.resolve.side_effect = resolve
    return manager


class TestPendingTranscriptRecheck:
    """Shared hook-seam pending transcript association."""

    def test_preinvocation_pending_then_stop_associates(
        self,
        manager_with_mocks: HookManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            manager_with_mocks,
            "get_machine_id",
            lambda: LOCAL_MACHINE_ID,
        )
        target = _agy_transcript(tmp_path)
        session = SimpleNamespace(
            id="platform-1",
            transcript_path=None,
            source="agy",
            external_id="conv-1",
            machine_id=LOCAL_MACHINE_ID,
        )
        manager = _pending_recheck_manager(manager_with_mocks, session=session)
        pending_event = HookEvent(
            event_type=HookEventType.AFTER_AGENT,
            session_id="conv-1",
            source=SessionSource.AGY,
            timestamp=datetime.now(UTC),
            data={"transcript_path": str(target)},
            project_id="proj-1",
            machine_id=LOCAL_MACHINE_ID,
        )
        with patch("gobby.hooks.hook_manager.reconcile_session_activation"):
            first = manager._handle_after_daemon_ready(pending_event)
        assert first.decision == "allow"
        cast(Any, manager._session_manager).update.assert_not_called()

        target.parent.mkdir(parents=True)
        target.write_text("{}\n", encoding="utf-8")
        stop_event = HookEvent(
            event_type=HookEventType.STOP,
            session_id="conv-1",
            source=SessionSource.AGY,
            timestamp=datetime.now(UTC),
            data={"transcript_path": str(target)},
            project_id="proj-1",
            machine_id=LOCAL_MACHINE_ID,
        )
        with patch("gobby.hooks.hook_manager.reconcile_session_activation"):
            second = manager._handle_after_daemon_ready(stop_event)
        assert second.decision == "allow"
        cast(Any, manager._session_manager).update.assert_called_once_with(
            "platform-1",
            transcript_path=str(target),
        )

    def test_already_persisted_path_skips_recheck(
        self,
        manager_with_mocks: HookManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            manager_with_mocks,
            "get_machine_id",
            lambda: LOCAL_MACHINE_ID,
        )
        target = tmp_path / "already.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        session = SimpleNamespace(
            id="platform-1",
            transcript_path=str(target),
            source="agy",
            external_id="conv-1",
            machine_id=LOCAL_MACHINE_ID,
        )
        manager = _pending_recheck_manager(manager_with_mocks, session=session)
        event = HookEvent(
            event_type=HookEventType.STOP,
            session_id="conv-1",
            source=SessionSource.AGY,
            timestamp=datetime.now(UTC),
            data={"transcript_path": str(tmp_path / "other.jsonl")},
            project_id="proj-1",
            machine_id=LOCAL_MACHINE_ID,
        )
        with patch("gobby.hooks.hook_manager.reconcile_session_activation"):
            response = manager._handle_after_daemon_ready(event)
        assert response.decision == "allow"
        assert session.transcript_path == str(target)
        assert manager._pending_transcript_rechecks == {}
        cast(Any, manager._session_manager).update.assert_not_called()
