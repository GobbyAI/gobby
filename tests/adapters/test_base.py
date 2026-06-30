"""Comprehensive tests for BaseAdapter abstract class.

Tests cover:
1. BaseAdapter abstract class behavior
2. Abstract method enforcement
3. Concrete method (handle_native) behavior
4. Error handling scenarios
5. Edge cases with None returns
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from gobby.adapters.base import BaseAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

pytestmark = pytest.mark.unit

# =============================================================================
# Test Fixtures and Concrete Implementation for Testing
# =============================================================================


class ConcreteAdapter(BaseAdapter):
    """Concrete implementation of BaseAdapter for testing.

    This adapter implements all abstract methods with configurable behavior
    for testing different scenarios.
    """

    source = SessionSource.CLAUDE

    def __init__(
        self,
        translate_result: HookEvent | None = None,
        response_result: dict | None = None,
    ):
        """Initialize the concrete test adapter.

        Args:
            translate_result: Value to return from translate_to_hook_event()
            response_result: Value to return from translate_from_hook_response()
        """
        self._translate_result = translate_result
        self._response_result = response_result or {}

    def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
        """Return pre-configured translation result."""
        return self._translate_result

    def translate_from_hook_response(self, response: HookResponse) -> dict:
        """Return pre-configured response result."""
        return self._response_result


class IncompleteAdapter(BaseAdapter):
    """Adapter that only implements some abstract methods (for testing)."""

    source = SessionSource.QWEN

    def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
        """Only implement one abstract method."""
        return None


class MissingSourceAdapter(BaseAdapter):
    """Adapter that implements methods but does not declare its source."""

    def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
        return None

    def translate_from_hook_response(self, response: HookResponse) -> dict:
        return {}


# =============================================================================
# Abstract Class Tests
# =============================================================================


class TestBaseAdapterAbstract:
    """Tests for BaseAdapter abstract class properties."""

    def test_cannot_instantiate_base_adapter_directly(self) -> None:
        """BaseAdapter cannot be instantiated directly."""
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            BaseAdapter()

    def test_cannot_instantiate_incomplete_adapter(self) -> None:
        """Adapter missing abstract methods cannot be instantiated."""
        # IncompleteAdapter is missing translate_from_hook_response
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteAdapter()

    def test_concrete_adapter_instantiation(self) -> None:
        """Concrete adapter with all methods can be instantiated."""
        adapter = ConcreteAdapter()
        assert adapter is not None

    def test_source_attribute_required(self) -> None:
        """Subclasses must define source attribute."""
        adapter = ConcreteAdapter()
        assert adapter.source == SessionSource.CLAUDE

    def test_cannot_instantiate_adapter_without_source(self) -> None:
        """Adapter missing source cannot be instantiated."""
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            MissingSourceAdapter()


class TestBaseAdapterSubclassing:
    """Tests for proper subclassing of BaseAdapter."""

    def test_subclass_inherits_handle_native(self) -> None:
        """Concrete subclass inherits handle_native method."""
        adapter = ConcreteAdapter()
        assert hasattr(adapter, "handle_native")
        assert callable(adapter.handle_native)

    def test_subclass_can_override_handle_native(self) -> None:
        """Concrete subclass can override handle_native."""

        class CustomAdapter(ConcreteAdapter):
            def handle_native(self, native_event: dict, hook_manager: "HookManager") -> dict:
                return {"overridden": True}

        adapter = CustomAdapter()
        mock_manager = MagicMock()

        result = adapter.handle_native({}, mock_manager)

        assert result == {"overridden": True}

    def test_different_sources_can_be_defined(self) -> None:
        """Different adapters can define different sources."""

        class QwenTestAdapter(BaseAdapter):
            source = SessionSource.QWEN

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                return None

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                return {}

        class CodexTestAdapter(BaseAdapter):
            source = SessionSource.CODEX

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                return None

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                return {}

        qwen_adapter = QwenTestAdapter()
        codex_adapter = CodexTestAdapter()

        assert qwen_adapter.source == SessionSource.QWEN
        assert codex_adapter.source == SessionSource.CODEX


# =============================================================================
# Abstract Method Behavior Tests
# =============================================================================


class TestTranslateToHookEvent:
    """Tests for translate_to_hook_event abstract method behavior."""

    @pytest.fixture
    def sample_hook_event(self):
        """Create a sample HookEvent for testing."""
        return HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="test-session-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"key": "value"},
        )

    def test_translate_returns_hook_event(self, sample_hook_event) -> None:
        """translate_to_hook_event returns HookEvent when successful."""
        adapter = ConcreteAdapter(translate_result=sample_hook_event)

        result = adapter.translate_to_hook_event({"test": "data"})

        assert result is sample_hook_event
        assert result.event_type == HookEventType.SESSION_START
        assert result.session_id == "test-session-123"

    def test_translate_can_return_none(self) -> None:
        """translate_to_hook_event can return None for ignored events."""
        adapter = ConcreteAdapter(translate_result=None)

        result = adapter.translate_to_hook_event({"ignored": "event"})

        assert result is None

    def test_translate_receives_native_event(self) -> None:
        """translate_to_hook_event receives the native event dict."""

        class InspectingAdapter(BaseAdapter):
            source = SessionSource.CLAUDE
            received_event = None

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                InspectingAdapter.received_event = native_event
                return None

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                return {}

        adapter = InspectingAdapter()
        test_event = {"hook_type": "test", "data": {"nested": "value"}}

        adapter.translate_to_hook_event(test_event)

        assert InspectingAdapter.received_event == test_event


class TestTranslateFromHookResponse:
    """Tests for translate_from_hook_response abstract method behavior."""

    def test_translate_returns_dict(self) -> None:
        """translate_from_hook_response returns a dict."""
        adapter = ConcreteAdapter(response_result={"decision": "allow"})

        response = HookResponse(decision="allow")
        result = adapter.translate_from_hook_response(response)

        assert isinstance(result, dict)
        assert result == {"decision": "allow"}

    def test_translate_can_return_empty_dict(self) -> None:
        """translate_from_hook_response can return empty dict."""
        adapter = ConcreteAdapter(response_result={})

        response = HookResponse(decision="allow")
        result = adapter.translate_from_hook_response(response)

        assert result == {}

    def test_translate_receives_hook_response(self) -> None:
        """translate_from_hook_response receives HookResponse object."""

        class InspectingAdapter(BaseAdapter):
            source = SessionSource.CLAUDE
            received_response = None

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                return None

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                InspectingAdapter.received_response = response
                return {"processed": True}

        adapter = InspectingAdapter()
        test_response = HookResponse(
            decision="deny",
            reason="Policy violation",
            context="Additional context",
        )

        adapter.translate_from_hook_response(test_response)

        assert InspectingAdapter.received_response is test_response
        assert InspectingAdapter.received_response.decision == "deny"
        assert InspectingAdapter.received_response.reason == "Policy violation"


# =============================================================================
# handle_native Method Tests
# =============================================================================


class TestHandleNative:
    """Tests for the concrete handle_native method."""

    @pytest.fixture
    def sample_hook_event(self):
        """Create a sample HookEvent for testing."""
        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="sess-handle-native",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Write"},
        )

    @pytest.fixture
    def mock_hook_manager(self):
        """Create a mock HookManager."""
        manager = MagicMock()
        manager.handle.return_value = HookResponse(decision="allow")
        return manager

    def test_handle_native_full_roundtrip(self, sample_hook_event, mock_hook_manager) -> None:
        """handle_native performs full translate -> process -> translate cycle."""
        adapter = ConcreteAdapter(
            translate_result=sample_hook_event,
            response_result={"decision": "allow", "continue": True},
        )

        result = adapter.handle_native({"native": "event"}, mock_hook_manager)

        # Verify HookManager.handle was called with translated event
        mock_hook_manager.handle.assert_called_once_with(sample_hook_event)

        # Verify response was translated
        assert result == {"decision": "allow", "continue": True}

    def test_handle_native_does_not_stamp_daemon_machine_id(
        self, sample_hook_event, mock_hook_manager
    ) -> None:
        """handle_native preserves client-provided machine identity only."""
        adapter = ConcreteAdapter(
            translate_result=sample_hook_event,
            response_result={"decision": "allow", "continue": True},
        )

        with patch(
            "gobby.utils.machine_id.get_machine_id", return_value="daemon-machine"
        ) as get_id:
            adapter.handle_native({"native": "event"}, mock_hook_manager)

        get_id.assert_not_called()
        handled_event = mock_hook_manager.handle.call_args.args[0]
        assert handled_event.machine_id is None

    def test_handle_native_returns_empty_when_event_is_none(self, mock_hook_manager) -> None:
        """handle_native returns empty dict when translate returns None."""
        adapter = ConcreteAdapter(translate_result=None)

        result = adapter.handle_native({"ignored": "event"}, mock_hook_manager)

        # HookManager should not be called for ignored events
        mock_hook_manager.handle.assert_not_called()

        # Should return empty dict
        assert result == {}

    def test_handle_native_passes_hook_response_to_translate(self, sample_hook_event) -> None:
        """handle_native passes HookResponse from manager to translate method."""
        expected_response = HookResponse(
            decision="deny",
            reason="Task not claimed",
            context="Please claim a task first",
        )

        mock_manager = MagicMock()
        mock_manager.handle.return_value = expected_response

        class VerifyingAdapter(BaseAdapter):
            source = SessionSource.CLAUDE
            received_response = None

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                return sample_hook_event

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                VerifyingAdapter.received_response = response
                return {"decision": response.decision}

        adapter = VerifyingAdapter()

        result = adapter.handle_native({}, mock_manager)

        assert VerifyingAdapter.received_response is expected_response
        assert result["decision"] == "deny"

    def test_handle_native_with_various_response_decisions(self, sample_hook_event) -> None:
        """handle_native works with all response decision types."""
        decisions = ["allow", "deny", "ask", "block", "modify"]

        for decision in decisions:
            mock_manager = MagicMock()
            mock_manager.handle.return_value = HookResponse(decision=decision)

            adapter = ConcreteAdapter(
                translate_result=sample_hook_event,
                response_result={"decision": decision},
            )

            result = adapter.handle_native({}, mock_manager)

            assert result["decision"] == decision

    def test_handle_native_preserves_native_event(
        self, sample_hook_event, mock_hook_manager
    ) -> None:
        """handle_native passes native event unchanged to translate."""
        received_events = []

        class TrackingAdapter(BaseAdapter):
            source = SessionSource.CLAUDE

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                received_events.append(native_event)
                return sample_hook_event

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                return {}

        adapter = TrackingAdapter()
        test_event = {
            "hook_type": "pre-tool-use",
            "input_data": {
                "session_id": "abc",
                "tool_name": "Write",
                "nested": {"deep": {"value": 42}},
            },
        }

        adapter.handle_native(test_event, mock_hook_manager)

        assert len(received_events) == 1
        assert received_events[0] == test_event
        assert received_events[0]["input_data"]["nested"]["deep"]["value"] == 42


class TestHandleNativeEdgeCases:
    """Edge case tests for handle_native method."""

    @pytest.fixture
    def sample_hook_event(self):
        """Create a sample HookEvent for testing."""
        return HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="edge-case-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
        )

    def test_handle_native_with_empty_native_event(self, sample_hook_event) -> None:
        """handle_native works with empty native event dict."""
        mock_manager = MagicMock()
        mock_manager.handle.return_value = HookResponse(decision="allow")

        adapter = ConcreteAdapter(
            translate_result=sample_hook_event,
            response_result={"status": "ok"},
        )

        result = adapter.handle_native({}, mock_manager)

        assert result == {"status": "ok"}

    def test_handle_native_with_complex_response(self, sample_hook_event) -> None:
        """handle_native handles complex translated responses."""
        mock_manager = MagicMock()
        mock_manager.handle.return_value = HookResponse(
            decision="allow",
            context="Injected context",
            system_message="User notification",
            modify_args={"temperature": 0.5},
            metadata={"custom": "data"},
        )

        complex_result = {
            "decision": "allow",
            "hookSpecificOutput": {
                "additionalContext": "Injected context",
                "llm_request": {"temperature": 0.5},
            },
            "systemMessage": "User notification",
        }

        adapter = ConcreteAdapter(
            translate_result=sample_hook_event,
            response_result=complex_result,
        )

        result = adapter.handle_native({"test": True}, mock_manager)

        assert result == complex_result
        assert "hookSpecificOutput" in result
        assert result["hookSpecificOutput"]["additionalContext"] == "Injected context"

    def test_handle_native_multiple_calls(self, sample_hook_event) -> None:
        """handle_native can be called multiple times."""
        mock_manager = MagicMock()
        mock_manager.handle.return_value = HookResponse(decision="allow")

        adapter = ConcreteAdapter(
            translate_result=sample_hook_event,
            response_result={"count": 1},
        )

        # Call multiple times
        results = []
        for _ in range(3):
            result = adapter.handle_native({}, mock_manager)
            results.append(result)

        assert len(results) == 3
        assert mock_manager.handle.call_count == 3


# =============================================================================
# Integration Tests
# =============================================================================


class TestAdapterIntegration:
    """Integration tests for adapter behavior with realistic scenarios."""

    def test_session_lifecycle_simulation(self) -> None:
        """Simulate full session lifecycle through adapter."""
        events_processed = []

        class LifecycleAdapter(BaseAdapter):
            source = SessionSource.CLAUDE

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                event_type_map = {
                    "session-start": HookEventType.SESSION_START,
                    "pre-tool-use": HookEventType.BEFORE_TOOL,
                    "post-tool-use": HookEventType.AFTER_TOOL,
                    "session-end": HookEventType.SESSION_END,
                }
                hook_type = native_event.get("hook_type")
                if hook_type not in event_type_map:
                    return None

                events_processed.append(hook_type)
                return HookEvent(
                    event_type=event_type_map[hook_type],
                    session_id=native_event.get("session_id", "test-sess"),
                    source=SessionSource.CLAUDE,
                    timestamp=datetime.now(UTC),
                    data=native_event.get("input_data", {}),
                )

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                return {"continue": response.decision == "allow"}

        adapter = LifecycleAdapter()
        mock_manager = MagicMock()
        mock_manager.handle.return_value = HookResponse(decision="allow")

        # Simulate session lifecycle
        session_events = [
            {"hook_type": "session-start", "session_id": "sess-1"},
            {"hook_type": "pre-tool-use", "session_id": "sess-1"},
            {"hook_type": "post-tool-use", "session_id": "sess-1"},
            {"hook_type": "pre-tool-use", "session_id": "sess-1"},
            {"hook_type": "post-tool-use", "session_id": "sess-1"},
            {"hook_type": "session-end", "session_id": "sess-1"},
        ]

        for event in session_events:
            result = adapter.handle_native(event, mock_manager)
            assert result["continue"] is True

        assert events_processed == [
            "session-start",
            "pre-tool-use",
            "post-tool-use",
            "pre-tool-use",
            "post-tool-use",
            "session-end",
        ]
        assert mock_manager.handle.call_count == 6

    def test_tool_blocking_scenario(self) -> None:
        """Simulate tool being blocked by hook manager."""

        class BlockingAdapter(BaseAdapter):
            source = SessionSource.CLAUDE

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                return HookEvent(
                    event_type=HookEventType.BEFORE_TOOL,
                    session_id="blocking-session",
                    source=SessionSource.CLAUDE,
                    timestamp=datetime.now(UTC),
                    data=native_event.get("input_data", {}),
                )

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                result = {"continue": response.decision == "allow"}
                if response.reason:
                    result["stopReason"] = response.reason
                if response.system_message:
                    result["systemMessage"] = response.system_message
                return result

        adapter = BlockingAdapter()
        mock_manager = MagicMock()
        mock_manager.handle.return_value = HookResponse(
            decision="deny",
            reason="No task claimed",
            system_message="Please claim a task before editing files",
        )

        result = adapter.handle_native(
            {"input_data": {"tool_name": "Write"}},
            mock_manager,
        )

        assert result["continue"] is False
        assert result["stopReason"] == "No task claimed"
        assert "Please claim a task" in result["systemMessage"]

    def test_context_injection_scenario(self) -> None:
        """Simulate context being injected via hook response."""

        class ContextAdapter(BaseAdapter):
            source = SessionSource.CLAUDE

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                return HookEvent(
                    event_type=HookEventType.SESSION_START,
                    session_id="context-session",
                    source=SessionSource.CLAUDE,
                    timestamp=datetime.now(UTC),
                    data={},
                )

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                result = {"continue": True}
                if response.context:
                    result["result"] = response.context
                return result

        adapter = ContextAdapter()
        mock_manager = MagicMock()
        mock_manager.handle.return_value = HookResponse(
            decision="allow",
            context="## Continuation Context\nYou have 3 pending tasks.",
        )

        result = adapter.handle_native({}, mock_manager)

        assert result["continue"] is True
        assert "Continuation Context" in result["result"]
        assert "3 pending tasks" in result["result"]


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestAdapterErrorHandling:
    """Tests for error handling in adapter methods."""

    def test_adapter_handles_manager_exception(self) -> None:
        """Test behavior when hook manager raises exception."""

        class SafeAdapter(BaseAdapter):
            source = SessionSource.CLAUDE

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                return HookEvent(
                    event_type=HookEventType.SESSION_START,
                    session_id="error-session",
                    source=SessionSource.CLAUDE,
                    timestamp=datetime.now(UTC),
                    data={},
                )

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                return {"status": "ok"}

        adapter = SafeAdapter()
        mock_manager = MagicMock()
        mock_manager.handle.side_effect = Exception("Manager error")

        # The base handle_native doesn't catch exceptions - this documents behavior
        with pytest.raises(Exception, match="Manager error"):
            adapter.handle_native({}, mock_manager)

    def test_translate_method_exception_propagates(self) -> None:
        """Test that exceptions in translate methods propagate."""

        class FailingAdapter(BaseAdapter):
            source = SessionSource.CLAUDE

            def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
                raise ValueError("Translation failed")

            def translate_from_hook_response(self, response: HookResponse) -> dict:
                return {}

        adapter = FailingAdapter()
        mock_manager = MagicMock()

        with pytest.raises(ValueError, match="Translation failed"):
            adapter.handle_native({}, mock_manager)


# =============================================================================
# Documentation Tests
# =============================================================================


class TestAdapterDocumentation:
    """Tests verifying documentation and docstrings are accurate."""

    def test_base_adapter_has_docstring(self) -> None:
        """BaseAdapter class has documentation."""
        assert BaseAdapter.__doc__ is not None
        assert "CLI adapters" in BaseAdapter.__doc__

    def test_translate_to_hook_event_has_docstring(self) -> None:
        """translate_to_hook_event has documentation."""
        assert BaseAdapter.translate_to_hook_event.__doc__ is not None
        assert "native" in BaseAdapter.translate_to_hook_event.__doc__.lower()

    def test_translate_from_hook_response_has_docstring(self) -> None:
        """translate_from_hook_response has documentation."""
        assert BaseAdapter.translate_from_hook_response.__doc__ is not None
        assert "response" in BaseAdapter.translate_from_hook_response.__doc__.lower()

    def test_handle_native_has_docstring(self) -> None:
        """handle_native has documentation."""
        assert BaseAdapter.handle_native.__doc__ is not None
        assert "entry point" in BaseAdapter.handle_native.__doc__.lower()
