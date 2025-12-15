"""Tests for hook event models."""

from datetime import datetime

from gobby.hooks.events import (
    EVENT_TYPE_CLI_SUPPORT,
    HookEvent,
    HookEventType,
    HookResponse,
    SessionSource,
)


class TestHookEventType:
    """Tests for HookEventType enum."""

    def test_session_lifecycle_events(self):
        """Test session lifecycle event types."""
        assert HookEventType.SESSION_START.value == "session_start"
        assert HookEventType.SESSION_END.value == "session_end"

    def test_agent_lifecycle_events(self):
        """Test agent lifecycle event types."""
        assert HookEventType.BEFORE_AGENT.value == "before_agent"
        assert HookEventType.AFTER_AGENT.value == "after_agent"

    def test_tool_lifecycle_events(self):
        """Test tool lifecycle event types."""
        assert HookEventType.BEFORE_TOOL.value == "before_tool"
        assert HookEventType.AFTER_TOOL.value == "after_tool"
        assert HookEventType.BEFORE_TOOL_SELECTION.value == "before_tool_selection"

    def test_model_lifecycle_events(self):
        """Test model lifecycle event types (Gemini only)."""
        assert HookEventType.BEFORE_MODEL.value == "before_model"
        assert HookEventType.AFTER_MODEL.value == "after_model"

    def test_other_events(self):
        """Test other event types."""
        assert HookEventType.PRE_COMPACT.value == "pre_compact"
        assert HookEventType.SUBAGENT_START.value == "subagent_start"
        assert HookEventType.SUBAGENT_STOP.value == "subagent_stop"
        assert HookEventType.NOTIFICATION.value == "notification"
        assert HookEventType.PERMISSION_REQUEST.value == "permission_request"

    def test_all_event_types_covered(self):
        """Test that all event types are in the support matrix."""
        for event_type in HookEventType:
            assert event_type in EVENT_TYPE_CLI_SUPPORT


class TestSessionSource:
    """Tests for SessionSource enum."""

    def test_source_values(self):
        """Test session source values."""
        assert SessionSource.CLAUDE.value == "claude"
        assert SessionSource.GEMINI.value == "gemini"
        assert SessionSource.CODEX.value == "codex"
        assert SessionSource.CLAUDE_SDK.value == "claude_sdk"


class TestHookEvent:
    """Tests for HookEvent dataclass."""

    def test_create_minimal_event(self):
        """Test creating event with required fields only."""
        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="test-session-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.utcnow(),
            data={},
        )

        assert event.event_type == HookEventType.SESSION_START
        assert event.session_id == "test-session-123"
        assert event.source == SessionSource.CLAUDE
        assert event.data == {}

    def test_create_full_event(self):
        """Test creating event with all fields."""
        now = datetime.utcnow()
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="full-session",
            source=SessionSource.GEMINI,
            timestamp=now,
            data={"tool_name": "bash", "args": ["ls", "-la"]},
            machine_id="machine-abc",
            cwd="/home/user/project",
            user_id="user-123",
            project_id="project-456",
            workflow_id="workflow-789",
            metadata={"custom": "value"},
        )

        assert event.event_type == HookEventType.BEFORE_TOOL
        assert event.machine_id == "machine-abc"
        assert event.cwd == "/home/user/project"
        assert event.user_id == "user-123"
        assert event.project_id == "project-456"
        assert event.workflow_id == "workflow-789"
        assert event.metadata == {"custom": "value"}
        assert event.data["tool_name"] == "bash"

    def test_default_optional_fields(self):
        """Test default values for optional fields."""
        event = HookEvent(
            event_type=HookEventType.SESSION_END,
            session_id="defaults-test",
            source=SessionSource.CODEX,
            timestamp=datetime.utcnow(),
            data={},
        )

        assert event.machine_id is None
        assert event.cwd is None
        assert event.user_id is None
        assert event.project_id is None
        assert event.workflow_id is None
        assert event.metadata == {}


class TestHookResponse:
    """Tests for HookResponse dataclass."""

    def test_default_response(self):
        """Test default response values."""
        response = HookResponse()

        assert response.decision == "allow"
        assert response.context is None
        assert response.system_message is None
        assert response.reason is None
        assert response.modify_args is None
        assert response.trigger_action is None
        assert response.metadata == {}

    def test_allow_response(self):
        """Test creating an allow response."""
        response = HookResponse(decision="allow")
        assert response.decision == "allow"

    def test_deny_response(self):
        """Test creating a deny response."""
        response = HookResponse(
            decision="deny",
            reason="Operation not permitted",
        )

        assert response.decision == "deny"
        assert response.reason == "Operation not permitted"

    def test_ask_response(self):
        """Test creating an ask response."""
        response = HookResponse(decision="ask")
        assert response.decision == "ask"

    def test_response_with_context(self):
        """Test response with context injection."""
        response = HookResponse(
            decision="allow",
            context="Previous session summary: User was working on auth module.",
        )

        assert response.context is not None
        assert "auth module" in response.context

    def test_response_with_system_message(self):
        """Test response with user-visible system message."""
        response = HookResponse(
            decision="allow",
            system_message="Context restored from previous session.",
        )

        assert response.system_message == "Context restored from previous session."

    def test_response_with_metadata(self):
        """Test response with custom metadata."""
        response = HookResponse(
            decision="allow",
            metadata={"session_id": "abc123", "restored": True},
        )

        assert response.metadata["session_id"] == "abc123"
        assert response.metadata["restored"] is True


class TestEventTypeCLISupport:
    """Tests for EVENT_TYPE_CLI_SUPPORT mapping."""

    def test_claude_support(self):
        """Test Claude Code support in mapping."""
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.SESSION_START]["claude"] == "SessionStart"
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_TOOL]["claude"] == "PreToolUse"
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.AFTER_TOOL]["claude"] == "PostToolUse"

    def test_gemini_support(self):
        """Test Gemini CLI support in mapping."""
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.SESSION_START]["gemini"] == "SessionStart"
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_TOOL]["gemini"] == "BeforeTool"
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_TOOL_SELECTION]["gemini"] == "BeforeToolSelection"

    def test_codex_support(self):
        """Test Codex CLI support in mapping."""
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.SESSION_START]["codex"] == "thread/started"
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_TOOL]["codex"] == "requestApproval"
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.AFTER_TOOL]["codex"] == "item/completed"

    def test_cli_specific_events(self):
        """Test CLI-specific event support."""
        # Gemini-only events
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_MODEL]["claude"] is None
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_MODEL]["gemini"] == "BeforeModel"
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_MODEL]["codex"] is None

        # Claude-only events
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.SUBAGENT_START]["claude"] == "SubagentStart"
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.SUBAGENT_START]["gemini"] is None
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.SUBAGENT_START]["codex"] is None
