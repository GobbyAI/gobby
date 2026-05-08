from datetime import UTC, datetime

import pytest

from gobby.hooks.events import (
    EVENT_TYPE_CLI_SUPPORT,
    HookEvent,
    HookEventType,
    HookResponse,
    SessionSource,
)

pytestmark = pytest.mark.unit


class TestHookEventType:
    """Tests for HookEventType enum."""

    def test_enum_values(self) -> None:
        """Test that key enum values exist."""
        assert HookEventType.SESSION_START == "session_start"
        assert HookEventType.BEFORE_TOOL == "before_tool"
        assert HookEventType.PERMISSION_REQUEST == "permission_request"


class TestSessionSource:
    """Tests for SessionSource enum."""

    def test_enum_values(self) -> None:
        """Test that source values exist."""
        assert SessionSource.CLAUDE == "claude"
        assert SessionSource.DROID == "droid"
        assert SessionSource.GEMINI == "gemini"


class TestHookEvent:
    """Tests for HookEvent dataclass."""

    def test_minimal_instantiation(self) -> None:
        """Test creating event with required fields only."""
        now = datetime.now(UTC)
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="sess-123",
            source=SessionSource.CLAUDE,
            timestamp=now,
            data={"foo": "bar"},
        )

        assert event.event_type == HookEventType.SESSION_START
        assert event.session_id == "sess-123"
        assert event.source == SessionSource.CLAUDE
        assert event.timestamp == now
        assert event.data == {"foo": "bar"}

        # Check defaults
        assert event.machine_id is None
        assert event.cwd is None
        assert event.metadata == {}

    def test_full_instantiation(self) -> None:
        """Test creating event with all fields."""
        now = datetime.now(UTC)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="sess-456",
            source=SessionSource.GEMINI,
            timestamp=now,
            data={"tool": "ls"},
            machine_id="machine-1",
            cwd="/tmp",
            user_id="user-1",
            project_id="proj-1",
            task_id="task-1",
            workflow_id="wf-1",
            metadata={"extra": "info"},
        )

        assert event.machine_id == "machine-1"
        assert event.cwd == "/tmp"
        assert event.user_id == "user-1"
        assert event.metadata == {"extra": "info"}


class TestHookResponse:
    """Tests for HookResponse dataclass."""

    def test_defaults(self) -> None:
        """Test default values."""
        resp = HookResponse()
        assert resp.decision == "allow"
        assert resp.context is None
        assert resp.metadata == {}

    def test_instantiation(self) -> None:
        """Test custom values."""
        resp = HookResponse(
            decision="deny",
            context="Stop doing that",
            system_message="Action blocked",
            reason="Policy violation",
            modify_args={"arg": "val"},
            trigger_action="notify",
            metadata={"rule": "123"},
        )

        assert resp.decision == "deny"
        assert resp.context == "Stop doing that"
        assert resp.system_message == "Action blocked"
        assert resp.reason == "Policy violation"
        assert resp.modify_args == {"arg": "val"}
        assert resp.trigger_action == "notify"
        assert resp.metadata == {"rule": "123"}


class TestEventTypeMapping:
    """Tests for EVENT_TYPE_CLI_SUPPORT constant."""

    def test_mapping_coverage(self) -> None:
        """Ensure mapping covers all enum types."""
        # This checks that we didn't forget to add a new enum member to the mapping table
        # if that is the intent.
        assert len(EVENT_TYPE_CLI_SUPPORT) == len(HookEventType)

        for event_type in HookEventType:
            assert event_type in EVENT_TYPE_CLI_SUPPORT
            assert "claude" in EVENT_TYPE_CLI_SUPPORT[event_type]
            assert "gemini" in EVENT_TYPE_CLI_SUPPORT[event_type]
            assert "codex" in EVENT_TYPE_CLI_SUPPORT[event_type]
