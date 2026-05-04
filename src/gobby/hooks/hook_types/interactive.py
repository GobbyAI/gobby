"""Interactive hook models: elicitation, model inference (Gemini), permissions (Claude)."""

from typing import Any

from pydantic import Field

from .base import HookInput, HookOutput


class ElicitationInput(HookInput):
    """Input model for elicitation hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    mcp_server_name: str = Field(..., min_length=1, description="MCP server name")
    message: str = Field(..., min_length=1, description="Prompt shown to the user")
    mode: str | None = Field(default=None, description="Elicitation mode")
    url: str | None = Field(default=None, description="Browser URL for url-mode elicitation")
    elicitation_id: str | None = Field(default=None, description="Elicitation identifier")
    requested_schema: dict[str, Any] | None = Field(
        default=None,
        description="Requested form schema for form-mode elicitation",
    )
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class ElicitationOutput(HookOutput):
    """Output model for elicitation hook."""

    action: str | None = Field(default=None, description="accept/decline/cancel decision")
    content: dict[str, Any] | None = Field(
        default=None,
        description="Form field values to submit for accept actions",
    )
    error_message: str | None = Field(
        default=None,
        alias="errorMessage",
        description="Optional error surfaced to the user",
    )


class ElicitationResultInput(HookInput):
    """Input model for elicitation-result hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    mcp_server_name: str = Field(..., min_length=1, description="MCP server name")
    action: str = Field(..., min_length=1, description="User action for the elicitation")
    mode: str | None = Field(default=None, description="Elicitation mode")
    elicitation_id: str | None = Field(default=None, description="Elicitation identifier")
    content: dict[str, Any] | None = Field(default=None, description="User-provided form data")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class ElicitationResultOutput(HookOutput):
    """Output model for elicitation-result hook."""

    action: str | None = Field(default=None, description="accept/decline/cancel override")
    content: dict[str, Any] | None = Field(
        default=None,
        description="Replacement form field values",
    )


class BeforeModelInput(HookInput):
    """
    Input model for before-model hook.

    Triggered before model inference (Gemini only). Can be used to
    modify or inspect prompts before they are sent to the model.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    model_name: str | None = Field(default=None, description="Name of the model being used")
    prompt: str | None = Field(default=None, description="Prompt being sent to model")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class BeforeModelOutput(HookOutput):
    """Output model for before-model hook."""

    pass  # Uses base HookOutput fields only


class AfterModelInput(HookInput):
    """
    Input model for after-model hook.

    Triggered after model inference (Gemini only). Can be used to
    inspect or log model responses.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    model_name: str | None = Field(default=None, description="Name of the model used")
    response: str | None = Field(default=None, description="Model response")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class AfterModelOutput(HookOutput):
    """Output model for after-model hook."""

    pass  # Uses base HookOutput fields only


class PermissionRequestInput(HookInput):
    """
    Input model for permission-request hook.

    Triggered when Claude Code requests permission for an action (Claude only).
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    tool_name: str = Field(..., min_length=1, description="Name of the tool requesting access")
    tool_input: dict[str, Any] = Field(default_factory=dict, description="Pending tool input")
    permission_suggestions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Suggested permission updates Claude generated for the prompt",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class PermissionRequestOutput(HookOutput):
    """
    Output model for permission-request hook.

    Returns a nested permission decision object.
    """

    decision_payload: dict[str, Any] | None = Field(
        default=None,
        alias="decision",
        description="Claude permission decision object",
    )


class PermissionDeniedInput(HookInput):
    """Input model for permission-denied hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    tool_name: str = Field(..., min_length=1, description="Denied tool name")
    tool_input: dict[str, Any] = Field(default_factory=dict, description="Denied tool input")
    tool_use_id: str | None = Field(default=None, description="Claude tool use identifier")
    reason: str = Field(..., min_length=1, description="Classifier explanation for the denial")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class PermissionDeniedOutput(HookOutput):
    """Output model for permission-denied hook."""

    retry: bool = Field(default=False, description="Whether Claude may retry the tool call")
