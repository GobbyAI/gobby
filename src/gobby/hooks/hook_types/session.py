"""Session lifecycle hook models: session-start, session-end, user-prompt-submit."""

from typing import Any

from pydantic import Field

from .base import HookInput, HookOutput
from .enums import SessionEndReason, SessionStartSource


class SessionStartInput(HookInput):
    """
    Input model for session-start hook.

    Triggered when a Claude Code session starts. Contains session metadata
    and context about how the session was initiated.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    transcript_path: str | None = Field(
        default=None, description="Path to conversation transcript file (Claude Code only)"
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

    context: dict[str, Any] = Field(default_factory=dict, description="Legacy session context")
    additional_context: str | None = Field(
        default=None,
        alias="additionalContext",
        description="Context injected into Claude at session start",
    )


class SetupInput(HookInput):
    """Input model for Claude's setup hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    trigger: str = Field(..., min_length=1, description="Setup trigger (init or maintenance)")
    transcript_path: str | None = Field(default=None, description="Path to transcript file")
    cwd: str | None = Field(default=None, description="Current working directory")
    permission_mode: str | None = Field(default=None, description="Active permission mode")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class SetupOutput(HookOutput):
    """Output model for Claude's setup hook."""

    additional_context: str | None = Field(default=None, alias="additionalContext")


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
    additional_context: str | None = Field(
        default=None,
        alias="additionalContext",
        description="Context injected into Claude for the submitted prompt",
    )
    session_title: str | None = Field(
        default=None,
        alias="sessionTitle",
        description="Optional session title override",
    )


class UserPromptExpansionInput(HookInput):
    """Input model for Claude prompt expansion hooks."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    expansion_type: str = Field(..., min_length=1, description="Expansion source type")
    command_name: str = Field(..., min_length=1, description="Expanded command name")
    command_args: str | None = Field(default=None, description="Command arguments")
    command_source: str | None = Field(default=None, description="Command definition source")
    prompt: str = Field(..., description="Expanded prompt")
    transcript_path: str | None = Field(default=None, description="Path to transcript file")
    cwd: str | None = Field(default=None, description="Current working directory")
    permission_mode: str | None = Field(default=None, description="Active permission mode")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class UserPromptExpansionOutput(HookOutput):
    """Output model for Claude prompt expansion hooks."""

    additional_context: str | None = Field(default=None, alias="additionalContext")


class MessageDisplayInput(HookInput):
    """Input model for Claude message display hooks."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    turn_id: str = Field(..., min_length=1, description="Rendered turn identifier")
    message_id: str = Field(..., min_length=1, description="Rendered message identifier")
    index: int = Field(..., ge=0, description="Delta index within the message")
    final: bool = Field(..., description="Whether this is the final delta")
    delta: str = Field(..., description="Original display delta")
    transcript_path: str | None = Field(default=None, description="Path to transcript file")
    cwd: str | None = Field(default=None, description="Current working directory")
    permission_mode: str | None = Field(default=None, description="Active permission mode")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class MessageDisplayOutput(HookOutput):
    """Output model for Claude message display hooks."""

    display_content: str | None = Field(default=None, alias="displayContent")
