"""Tool execution hook models: pre-tool-use, post-tool-use, post-tool-use-failure."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .base import HookInput, HookOutput


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
    permission_decision: str | None = Field(
        default=None,
        alias="permissionDecision",
        description="Allow/deny/ask/defer decision for the pending tool",
    )
    permission_decision_reason: str | None = Field(
        default=None,
        alias="permissionDecisionReason",
        description="Reason associated with the tool decision",
    )
    updated_input: dict[str, Any] | None = Field(
        default=None,
        alias="updatedInput",
        description="Replacement tool input payload",
    )
    additional_context: str | None = Field(
        default=None,
        alias="additionalContext",
        description="Context injected before the tool executes",
    )


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

    additional_context: str | None = Field(
        default=None,
        alias="additionalContext",
        description="Context injected after the tool completes",
    )


class PostToolUseFailureInput(HookInput):
    """Input model for post-tool-use-failure hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    tool_name: str = Field(..., min_length=1, description="Name of tool that failed")
    tool_input: dict[str, Any] = Field(default_factory=dict, description="Tool input parameters")
    tool_use_id: str | None = Field(default=None, description="Claude tool use identifier")
    error: str = Field(..., min_length=1, description="Description of the tool failure")
    is_interrupt: bool | None = Field(
        default=None,
        description="Whether the failure was caused by user interruption",
    )
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class PostToolUseFailureOutput(HookOutput):
    """Output model for post-tool-use-failure hook."""

    additional_context: str | None = Field(
        default=None,
        alias="additionalContext",
        description="Context injected after the tool failure",
    )
