"""Tests for src/hooks/hook_types.py - Hook Type Definitions."""

import pytest
from pydantic import ValidationError

from gobby.hooks.hook_types import (
    # Enums
    HookType,
    SessionStartSource,
    SessionEndReason,
    CompactTrigger,
    NotificationSeverity,
    # Base models
    HookInput,
    HookOutput,
    # Session Start
    SessionStartInput,
    SessionStartOutput,
    # Session End
    SessionEndInput,
    SessionEndOutput,
    # User Prompt Submit
    UserPromptSubmitInput,
    UserPromptSubmitOutput,
    # Pre/Post Tool Use
    PreToolUseInput,
    PreToolUseOutput,
    ContextItem,
    PostToolUseInput,
    PostToolUseOutput,
    # Pre-Compact
    PreCompactInput,
    PreCompactOutput,
    # Stop
    StopInput,
    StopOutput,
    # Subagent
    SubagentStartInput,
    SubagentStartOutput,
    SubagentStopInput,
    SubagentStopOutput,
    # Notification
    NotificationInput,
    NotificationOutput,
    # Mappings
    HOOK_INPUT_MODELS,
    HOOK_OUTPUT_MODELS,
)


class TestHookTypeEnum:
    """Tests for HookType enum."""

    def test_all_hook_types_defined(self):
        """Test that all expected hook types are defined."""
        expected_types = {
            "SESSION_START",
            "SESSION_END",
            "USER_PROMPT_SUBMIT",
            "PRE_TOOL_USE",
            "POST_TOOL_USE",
            "PRE_COMPACT",
            "STOP",
            "SUBAGENT_START",
            "SUBAGENT_STOP",
            "NOTIFICATION",
        }
        actual_types = {t.name for t in HookType}
        assert actual_types == expected_types

    def test_hook_type_values(self):
        """Test that hook type values use kebab-case."""
        assert HookType.SESSION_START.value == "session-start"
        assert HookType.SESSION_END.value == "session-end"
        assert HookType.USER_PROMPT_SUBMIT.value == "user-prompt-submit"
        assert HookType.PRE_TOOL_USE.value == "pre-tool-use"
        assert HookType.POST_TOOL_USE.value == "post-tool-use"
        assert HookType.PRE_COMPACT.value == "pre-compact"
        assert HookType.STOP.value == "stop"
        assert HookType.SUBAGENT_START.value == "subagent-start"
        assert HookType.SUBAGENT_STOP.value == "subagent-stop"
        assert HookType.NOTIFICATION.value == "notification"


class TestSessionStartSourceEnum:
    """Tests for SessionStartSource enum."""

    def test_all_sources_defined(self):
        """Test that all session start sources are defined."""
        expected = {"STARTUP", "RESUME", "CLEAR", "COMPACT"}
        actual = {s.name for s in SessionStartSource}
        assert actual == expected

    def test_source_values(self):
        """Test source enum values."""
        assert SessionStartSource.STARTUP.value == "startup"
        assert SessionStartSource.RESUME.value == "resume"
        assert SessionStartSource.CLEAR.value == "clear"
        assert SessionStartSource.COMPACT.value == "compact"


class TestSessionEndReasonEnum:
    """Tests for SessionEndReason enum."""

    def test_all_reasons_defined(self):
        """Test that all session end reasons are defined."""
        expected = {"CLEAR", "LOGOUT", "PROMPT_INPUT_EXIT", "OTHER"}
        actual = {r.name for r in SessionEndReason}
        assert actual == expected


class TestCompactTriggerEnum:
    """Tests for CompactTrigger enum."""

    def test_trigger_values(self):
        """Test compact trigger values."""
        assert CompactTrigger.AUTO.value == "auto"
        assert CompactTrigger.MANUAL.value == "manual"


class TestNotificationSeverityEnum:
    """Tests for NotificationSeverity enum."""

    def test_severity_values(self):
        """Test notification severity values."""
        assert NotificationSeverity.INFO.value == "info"
        assert NotificationSeverity.WARNING.value == "warning"
        assert NotificationSeverity.ERROR.value == "error"


class TestHookInput:
    """Tests for HookInput base model."""

    def test_allows_extra_fields(self):
        """Test that extra fields are allowed."""
        input_data = HookInput(extra_field="value")
        assert input_data.extra_field == "value"

    def test_strips_whitespace(self):
        """Test that string whitespace is stripped."""
        # HookInput itself has no string fields, but subclasses should inherit this
        pass


class TestHookOutput:
    """Tests for HookOutput base model."""

    def test_default_values(self):
        """Test default output values."""
        output = HookOutput()
        assert output.status == "success"
        assert output.message is None

    def test_custom_values(self):
        """Test custom output values."""
        output = HookOutput(status="error", message="Something went wrong")
        assert output.status == "error"
        assert output.message == "Something went wrong"


class TestSessionStartInput:
    """Tests for SessionStartInput model."""

    def test_required_fields(self):
        """Test that required fields are enforced."""
        with pytest.raises(ValidationError):
            SessionStartInput()  # Missing external_id and transcript_path

    def test_valid_input(self):
        """Test creating valid session start input."""
        input_data = SessionStartInput(
            external_id="test-key-123", transcript_path="/path/to/transcript.jsonl"
        )
        assert input_data.external_id == "test-key-123"
        assert input_data.transcript_path == "/path/to/transcript.jsonl"
        assert input_data.source == SessionStartSource.STARTUP  # Default

    def test_all_fields(self):
        """Test with all fields specified."""
        input_data = SessionStartInput(
            external_id="key",
            transcript_path="/path",
            source=SessionStartSource.RESUME,
            machine_id="machine-123",
            cwd="/home/user/project",
        )
        assert input_data.source == SessionStartSource.RESUME
        assert input_data.machine_id == "machine-123"
        assert input_data.cwd == "/home/user/project"

    def test_empty_external_id_rejected(self):
        """Test that empty external_id is rejected."""
        with pytest.raises(ValidationError):
            SessionStartInput(external_id="", transcript_path="/path")


class TestSessionStartOutput:
    """Tests for SessionStartOutput model."""

    def test_default_context(self):
        """Test default empty context."""
        output = SessionStartOutput()
        assert output.context == {}
        assert output.status == "success"

    def test_custom_context(self):
        """Test with custom context."""
        output = SessionStartOutput(context={"key": "value", "nested": {"a": 1}})
        assert output.context == {"key": "value", "nested": {"a": 1}}


class TestSessionEndInput:
    """Tests for SessionEndInput model."""

    def test_required_fields(self):
        """Test required external_id field."""
        with pytest.raises(ValidationError):
            SessionEndInput()

    def test_default_reason(self):
        """Test default reason is OTHER."""
        input_data = SessionEndInput(external_id="key")
        assert input_data.reason == SessionEndReason.OTHER

    def test_custom_reason(self):
        """Test custom reason."""
        input_data = SessionEndInput(external_id="key", reason=SessionEndReason.LOGOUT)
        assert input_data.reason == SessionEndReason.LOGOUT


class TestUserPromptSubmitInput:
    """Tests for UserPromptSubmitInput model."""

    def test_required_fields(self):
        """Test required fields."""
        with pytest.raises(ValidationError):
            UserPromptSubmitInput(external_id="key")  # Missing prompt_text

    def test_valid_input(self):
        """Test valid input."""
        input_data = UserPromptSubmitInput(external_id="key", prompt_text="What is the weather?")
        assert input_data.prompt_text == "What is the weather?"
        assert input_data.estimated_tokens is None
        assert input_data.metadata == {}

    def test_with_metadata(self):
        """Test with metadata."""
        input_data = UserPromptSubmitInput(
            external_id="key", prompt_text="test", metadata={"source": "web", "user_id": 123}
        )
        assert input_data.metadata["source"] == "web"


class TestUserPromptSubmitOutput:
    """Tests for UserPromptSubmitOutput model."""

    def test_default_allowed(self):
        """Test default is allowed."""
        output = UserPromptSubmitOutput()
        assert output.allowed is True
        assert output.block_message is None

    def test_blocked_output(self):
        """Test blocked output."""
        output = UserPromptSubmitOutput(allowed=False, block_message="This prompt violates policy")
        assert output.allowed is False
        assert output.block_message == "This prompt violates policy"


class TestPreToolUseInput:
    """Tests for PreToolUseInput model."""

    def test_required_fields(self):
        """Test required fields."""
        with pytest.raises(ValidationError):
            PreToolUseInput(external_id="key")  # Missing tool_name

    def test_valid_input(self):
        """Test valid input."""
        input_data = PreToolUseInput(external_id="key", tool_name="Bash")
        assert input_data.tool_name == "Bash"
        assert input_data.tool_input == {}

    def test_with_tool_input(self):
        """Test with tool input parameters."""
        input_data = PreToolUseInput(
            external_id="key", tool_name="Read", tool_input={"file_path": "/etc/passwd"}
        )
        assert input_data.tool_input["file_path"] == "/etc/passwd"


class TestContextItem:
    """Tests for ContextItem model."""

    def test_required_fields(self):
        """Test required fields."""
        with pytest.raises(ValidationError):
            ContextItem()

    def test_valid_item(self):
        """Test valid context item."""
        item = ContextItem(type="text", content="Important context")
        assert item.type == "text"
        assert item.content == "Important context"
        assert item.metadata == {}

    def test_with_metadata(self):
        """Test with metadata."""
        item = ContextItem(
            type="memory", content="Previous conversation about X", metadata={"relevance": 0.95}
        )
        assert item.metadata["relevance"] == 0.95


class TestPreToolUseOutput:
    """Tests for PreToolUseOutput model."""

    def test_default_empty_items(self):
        """Test default empty items list."""
        output = PreToolUseOutput()
        assert output.items == []

    def test_with_items(self):
        """Test with context items."""
        output = PreToolUseOutput(
            items=[
                ContextItem(type="text", content="Context 1"),
                ContextItem(type="code", content="def foo(): pass"),
            ]
        )
        assert len(output.items) == 2
        assert output.items[0].type == "text"


class TestPostToolUseInput:
    """Tests for PostToolUseInput model."""

    def test_valid_input(self):
        """Test valid input."""
        input_data = PostToolUseInput(
            external_id="key",
            tool_name="Write",
            tool_input={"file_path": "/tmp/test.txt"},
            transcript_path="/path/to/transcript.jsonl",
        )
        assert input_data.tool_name == "Write"
        assert input_data.transcript_path == "/path/to/transcript.jsonl"


class TestPreCompactInput:
    """Tests for PreCompactInput model."""

    def test_required_fields(self):
        """Test required fields."""
        with pytest.raises(ValidationError):
            PreCompactInput(external_id="key")  # Missing transcript_path

    def test_default_trigger(self):
        """Test default trigger is AUTO."""
        input_data = PreCompactInput(external_id="key", transcript_path="/path")
        assert input_data.trigger == CompactTrigger.AUTO

    def test_manual_trigger(self):
        """Test manual trigger with custom instructions."""
        input_data = PreCompactInput(
            external_id="key",
            transcript_path="/path",
            trigger=CompactTrigger.MANUAL,
            custom_instructions="Focus on authentication changes",
        )
        assert input_data.trigger == CompactTrigger.MANUAL
        assert input_data.custom_instructions == "Focus on authentication changes"


class TestPreCompactOutput:
    """Tests for PreCompactOutput model."""

    def test_default_summary(self):
        """Test default empty summary."""
        output = PreCompactOutput()
        assert output.summary == {}

    def test_with_summary(self):
        """Test with summary data."""
        output = PreCompactOutput(
            summary={"key_decisions": ["Use PostgreSQL"], "files_modified": ["src/main.py"]}
        )
        assert output.summary["key_decisions"] == ["Use PostgreSQL"]


class TestStopInput:
    """Tests for StopInput model."""

    def test_valid_input(self):
        """Test valid stop input."""
        input_data = StopInput(external_id="key", reason="User requested stop")
        assert input_data.reason == "User requested stop"
        assert input_data.metadata == {}


class TestSubagentStartInput:
    """Tests for SubagentStartInput model."""

    def test_required_fields(self):
        """Test required fields."""
        with pytest.raises(ValidationError):
            SubagentStartInput(external_id="key")  # Missing subagent_id

    def test_valid_input(self):
        """Test valid input."""
        input_data = SubagentStartInput(
            external_id="key",
            subagent_id="subagent-123",
            agent_id="agent-456",
            agent_transcript_path="/path/to/subagent.jsonl",
        )
        assert input_data.subagent_id == "subagent-123"
        assert input_data.agent_id == "agent-456"


class TestSubagentStopInput:
    """Tests for SubagentStopInput model."""

    def test_valid_input(self):
        """Test valid input."""
        input_data = SubagentStopInput(
            external_id="key", subagent_id="subagent-123", reason="Task completed"
        )
        assert input_data.subagent_id == "subagent-123"
        assert input_data.reason == "Task completed"


class TestNotificationInput:
    """Tests for NotificationInput model."""

    def test_required_fields(self):
        """Test required fields."""
        with pytest.raises(ValidationError):
            NotificationInput(external_id="key")  # Missing notification_type and message

    def test_valid_input(self):
        """Test valid notification input."""
        input_data = NotificationInput(
            external_id="key",
            notification_type="build_complete",
            message="Build finished successfully",
        )
        assert input_data.notification_type == "build_complete"
        assert input_data.severity == NotificationSeverity.INFO  # Default

    def test_error_severity(self):
        """Test error severity notification."""
        input_data = NotificationInput(
            external_id="key",
            notification_type="build_failed",
            message="Build failed with errors",
            severity=NotificationSeverity.ERROR,
        )
        assert input_data.severity == NotificationSeverity.ERROR


class TestHookMappings:
    """Tests for HOOK_INPUT_MODELS and HOOK_OUTPUT_MODELS mappings."""

    def test_all_hook_types_have_input_models(self):
        """Test that all hook types have input model mappings."""
        for hook_type in HookType:
            assert hook_type in HOOK_INPUT_MODELS, f"Missing input model for {hook_type}"

    def test_all_hook_types_have_output_models(self):
        """Test that all hook types have output model mappings."""
        for hook_type in HookType:
            assert hook_type in HOOK_OUTPUT_MODELS, f"Missing output model for {hook_type}"

    def test_input_model_mapping_correct(self):
        """Test that input model mapping returns correct types."""
        assert HOOK_INPUT_MODELS[HookType.SESSION_START] == SessionStartInput
        assert HOOK_INPUT_MODELS[HookType.SESSION_END] == SessionEndInput
        assert HOOK_INPUT_MODELS[HookType.USER_PROMPT_SUBMIT] == UserPromptSubmitInput
        assert HOOK_INPUT_MODELS[HookType.PRE_TOOL_USE] == PreToolUseInput
        assert HOOK_INPUT_MODELS[HookType.POST_TOOL_USE] == PostToolUseInput
        assert HOOK_INPUT_MODELS[HookType.PRE_COMPACT] == PreCompactInput
        assert HOOK_INPUT_MODELS[HookType.STOP] == StopInput
        assert HOOK_INPUT_MODELS[HookType.SUBAGENT_START] == SubagentStartInput
        assert HOOK_INPUT_MODELS[HookType.SUBAGENT_STOP] == SubagentStopInput
        assert HOOK_INPUT_MODELS[HookType.NOTIFICATION] == NotificationInput

    def test_output_model_mapping_correct(self):
        """Test that output model mapping returns correct types."""
        assert HOOK_OUTPUT_MODELS[HookType.SESSION_START] == SessionStartOutput
        assert HOOK_OUTPUT_MODELS[HookType.SESSION_END] == SessionEndOutput
        assert HOOK_OUTPUT_MODELS[HookType.USER_PROMPT_SUBMIT] == UserPromptSubmitOutput
        assert HOOK_OUTPUT_MODELS[HookType.PRE_TOOL_USE] == PreToolUseOutput
        assert HOOK_OUTPUT_MODELS[HookType.POST_TOOL_USE] == PostToolUseOutput
        assert HOOK_OUTPUT_MODELS[HookType.PRE_COMPACT] == PreCompactOutput
        assert HOOK_OUTPUT_MODELS[HookType.STOP] == StopOutput
        assert HOOK_OUTPUT_MODELS[HookType.SUBAGENT_START] == SubagentStartOutput
        assert HOOK_OUTPUT_MODELS[HookType.SUBAGENT_STOP] == SubagentStopOutput
        assert HOOK_OUTPUT_MODELS[HookType.NOTIFICATION] == NotificationOutput
