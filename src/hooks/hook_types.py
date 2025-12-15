"""
Hook Type Definitions and Pydantic Models.

This module defines all Claude Code hook types and their associated input/output models
using Pydantic for validation. Each hook type has specific input and output schemas
that ensure type safety and validation across the hook execution pipeline.

Hook Types (9 total):
1. session-start: Triggered when a Claude Code session starts
2. session-end: Triggered when a session ends
3. user-prompt-submit: Triggered before user prompt is submitted
4. pre-tool-use: Triggered before a tool is executed
5. post-tool-use: Triggered after a tool is executed
6. pre-compact: Triggered before context window compaction
7. stop: Triggered when agent stops
8. subagent-stop: Triggered when a subagent stops
9. notification: Triggered for system notifications

Example:
    ```python
    from gobby.hooks.hook_types import HookType, SessionStartInput

    # Validate input
    input_data = SessionStartInput(
        external_id="abc123",
        transcript_path="/path/to/transcript.jsonl",
        source="startup"
    )
    ```
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ==================== Enums ====================


class HookType(str, Enum):
    """
    Enumeration of all Claude Code hook types.

    Each hook type corresponds to a specific lifecycle event in Claude Code.
    Hook names use kebab-case to match Claude Code's hook naming convention.
    """

    SESSION_START = "session-start"
    """Triggered when a new Claude Code session starts (startup, resume, clear, compact)"""

    SESSION_END = "session-end"
    """Triggered when a Claude Code session ends (clear, logout, exit)"""

    USER_PROMPT_SUBMIT = "user-prompt-submit"
    """Triggered before user prompt is submitted for validation/filtering"""

    PRE_TOOL_USE = "pre-tool-use"
    """Triggered before a tool is executed (for context injection)"""

    POST_TOOL_USE = "post-tool-use"
    """Triggered after a tool is executed (for memory saving)"""

    PRE_COMPACT = "pre-compact"
    """Triggered before context window compaction (for summary generation)"""

    STOP = "stop"
    """Triggered when the main agent stops"""

    SUBAGENT_START = "subagent-start"
    """Triggered when a subagent (spawned via Task tool) starts"""

    SUBAGENT_STOP = "subagent-stop"
    """Triggered when a subagent (spawned via Task tool) stops"""

    NOTIFICATION = "notification"
    """Triggered for system notifications and alerts"""


class SessionStartSource(str, Enum):
    """Source trigger for session start events."""

    STARTUP = "startup"
    """New session started from scratch"""

    RESUME = "resume"
    """Session resumed from previous state"""

    CLEAR = "clear"
    """Session cleared and restarted"""

    COMPACT = "compact"
    """Session compacted and restarted"""


class SessionEndReason(str, Enum):
    """Reason for session end events."""

    CLEAR = "clear"
    """User cleared the session"""

    LOGOUT = "logout"
    """User logged out"""

    PROMPT_INPUT_EXIT = "prompt_input_exit"
    """User exited from prompt input"""

    OTHER = "other"
    """Other/unspecified reason"""


class CompactTrigger(str, Enum):
    """Trigger type for context compaction."""

    AUTO = "auto"
    """Automatic compaction triggered by token limit"""

    MANUAL = "manual"
    """Manual compaction triggered by user"""


class NotificationSeverity(str, Enum):
    """Severity level for notifications."""

    INFO = "info"
    """Informational notification"""

    WARNING = "warning"
    """Warning notification"""

    ERROR = "error"
    """Error notification"""


# ==================== Base Models ====================


class HookInput(BaseModel):
    """
    Base class for all hook input models.

    Provides common fields and configuration for hook inputs.
    All hook-specific input models should inherit from this base.
    """

    model_config = ConfigDict(
        extra="allow",  # Allow extra fields for future extensibility
        validate_assignment=True,  # Validate on attribute assignment
        str_strip_whitespace=True,  # Strip whitespace from strings
    )


class HookOutput(BaseModel):
    """
    Base class for all hook output models.

    Provides common fields for hook responses.
    All hook-specific output models should inherit from this base.
    """

    status: str = Field(default="success", description="Execution status (success/error/queued)")
    message: str | None = Field(default=None, description="Optional message or error details")

    model_config = ConfigDict(
        extra="allow",  # Allow extra fields for future extensibility
        validate_assignment=True,
    )


# ==================== Session Start Hook ====================


class SessionStartInput(HookInput):
    """
    Input model for session-start hook.

    Triggered when a Claude Code session starts. Contains session metadata
    and context about how the session was initiated.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    transcript_path: str = Field(
        ..., min_length=1, description="Path to conversation transcript file"
    )
    source: SessionStartSource = Field(
        default=SessionStartSource.STARTUP, description="Session start source trigger"
    )
    machine_id: str | None = Field(default=None, description="Unique machine identifier")
    cwd: str | None = Field(default=None, description="Current working directory")


class SessionStartOutput(HookOutput):
    """
    Output model for session-start hook.

    Returns session context to inject into Claude Code (if any).
    """

    context: dict[str, Any] = Field(default_factory=dict, description="Session context to inject")


# ==================== Session End Hook ====================


class SessionEndInput(HookInput):
    """Input model for session-end hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    reason: SessionEndReason = Field(
        default=SessionEndReason.OTHER, description="Reason for session end"
    )
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class SessionEndOutput(HookOutput):
    """Output model for session-end hook."""

    pass  # Uses base HookOutput fields only


# ==================== User Prompt Submit Hook ====================


class UserPromptSubmitInput(HookInput):
    """
    Input model for user-prompt-submit hook.

    Triggered before user prompt is submitted for validation/filtering.
    Can be used for cost estimation, content filtering, or rate limiting.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    prompt_text: str = Field(..., min_length=1, description="User's prompt text to validate")
    estimated_tokens: int | None = Field(default=None, ge=0, description="Estimated token count")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class UserPromptSubmitOutput(HookOutput):
    """
    Output model for user-prompt-submit hook.

    Returns validation result with allow/block decision.
    """

    allowed: bool = Field(default=True, description="Whether prompt is allowed to proceed")
    block_message: str | None = Field(
        default=None, description="Message to show user if blocked (required if allowed=False)"
    )


# ==================== Pre-Tool-Use Hook ====================


class PreToolUseInput(HookInput):
    """
    Input model for pre-tool-use hook.

    Triggered before a tool is executed. Can be used to inject relevant context
    based on the tool being used.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    tool_name: str = Field(..., min_length=1, description="Name of tool about to be used")
    tool_input: dict[str, Any] = Field(default_factory=dict, description="Tool input parameters")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class ContextItem(BaseModel):
    """A single context item to inject before tool execution."""

    type: str = Field(
        ..., min_length=1, description="Context item type (e.g., 'text', 'code', 'memory')"
    )
    content: str = Field(..., min_length=1, description="Context content")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    model_config = ConfigDict(extra="allow")


class PreToolUseOutput(HookOutput):
    """
    Output model for pre-tool-use hook.

    Returns context items to inject before tool execution.
    """

    items: list[ContextItem] = Field(default_factory=list, description="Context items to inject")


# ==================== Post-Tool-Use Hook ====================


class PostToolUseInput(HookInput):
    """
    Input model for post-tool-use hook.

    Triggered after a tool is executed. Can be used to save execution context
    for future retrieval.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    tool_name: str = Field(..., min_length=1, description="Name of tool that was executed")
    tool_input: dict[str, Any] = Field(default_factory=dict, description="Tool input parameters")
    transcript_path: str | None = Field(default=None, description="Path to transcript file")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class PostToolUseOutput(HookOutput):
    """
    Output model for post-tool-use hook.

    Fire-and-forget acknowledgment.
    """

    pass  # Uses base HookOutput fields (status="queued" typical)


# ==================== Pre-Compact Hook ====================


class PreCompactInput(HookInput):
    """
    Input model for pre-compact hook.

    Triggered before context window compaction. Can be used to generate
    summaries or save compaction checkpoints.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    transcript_path: str = Field(..., min_length=1, description="Path to conversation transcript")
    trigger: CompactTrigger = Field(
        default=CompactTrigger.AUTO, description="Compaction trigger type"
    )
    custom_instructions: str | None = Field(
        default=None, description="Custom instructions if manually triggered"
    )
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class PreCompactOutput(HookOutput):
    """
    Output model for pre-compact hook.

    Returns summary data for compaction.
    """

    summary: dict[str, Any] = Field(default_factory=dict, description="Summary data for compaction")


# ==================== Stop Hook ====================


class StopInput(HookInput):
    """
    Input model for stop hook.

    Triggered when the main agent stops. Can be used for cleanup and
    final state persistence.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    reason: str | None = Field(default=None, description="Reason for stopping")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class StopOutput(HookOutput):
    """Output model for stop hook."""

    pass  # Uses base HookOutput fields only


# ==================== Subagent Stop Hook ====================


# ==================== Subagent Start Hook ====================


class SubagentStartInput(HookInput):
    """
    Input model for subagent-start hook.

    Triggered when a subagent (spawned via Task tool) starts.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    subagent_id: str = Field(..., min_length=1, description="Unique subagent identifier")
    agent_id: str | None = Field(default=None, description="Agent ID of the subagent")
    agent_transcript_path: str | None = Field(
        default=None, description="Path to the subagent's transcript file"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class SubagentStartOutput(HookOutput):
    """Output model for subagent-start hook."""

    pass  # Uses base HookOutput fields only


class SubagentStopInput(HookInput):
    """
    Input model for subagent-stop hook.

    Triggered when a subagent (spawned via Task tool) stops.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    subagent_id: str = Field(..., min_length=1, description="Unique subagent identifier")
    agent_id: str | None = Field(default=None, description="Agent ID of the subagent")
    agent_transcript_path: str | None = Field(
        default=None, description="Path to the subagent's transcript file"
    )
    reason: str | None = Field(default=None, description="Reason for stopping subagent")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class SubagentStopOutput(HookOutput):
    """Output model for subagent-stop hook."""

    pass  # Uses base HookOutput fields only


# ==================== Notification Hook ====================


class NotificationInput(HookInput):
    """
    Input model for notification hook.

    Triggered for system notifications and alerts.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    notification_type: str = Field(..., min_length=1, description="Type of notification")
    message: str = Field(..., min_length=1, description="Notification message")
    severity: NotificationSeverity = Field(
        default=NotificationSeverity.INFO, description="Severity level"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class NotificationOutput(HookOutput):
    """Output model for notification hook."""

    pass  # Uses base HookOutput fields (status="queued" typical)


# ==================== Type Mappings ====================

# Mapping of hook types to their input/output model classes
HOOK_INPUT_MODELS: dict[HookType, type[HookInput]] = {
    HookType.SESSION_START: SessionStartInput,
    HookType.SESSION_END: SessionEndInput,
    HookType.USER_PROMPT_SUBMIT: UserPromptSubmitInput,
    HookType.PRE_TOOL_USE: PreToolUseInput,
    HookType.POST_TOOL_USE: PostToolUseInput,
    HookType.PRE_COMPACT: PreCompactInput,
    HookType.STOP: StopInput,
    HookType.SUBAGENT_START: SubagentStartInput,
    HookType.SUBAGENT_STOP: SubagentStopInput,
    HookType.NOTIFICATION: NotificationInput,
}

HOOK_OUTPUT_MODELS: dict[HookType, type[HookOutput]] = {
    HookType.SESSION_START: SessionStartOutput,
    HookType.SESSION_END: SessionEndOutput,
    HookType.USER_PROMPT_SUBMIT: UserPromptSubmitOutput,
    HookType.PRE_TOOL_USE: PreToolUseOutput,
    HookType.POST_TOOL_USE: PostToolUseOutput,
    HookType.PRE_COMPACT: PreCompactOutput,
    HookType.STOP: StopOutput,
    HookType.SUBAGENT_START: SubagentStartOutput,
    HookType.SUBAGENT_STOP: SubagentStopOutput,
    HookType.NOTIFICATION: NotificationOutput,
}
