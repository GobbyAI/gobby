"""
Hook Type Definitions and Pydantic Models.

This module defines all Claude Code hook types and their associated input/output models
using Pydantic for validation. Each hook type has specific input and output schemas
that ensure type safety and validation across the hook execution pipeline.

Hook Types (27 total):
1. session-start: Triggered when a Claude Code session starts
2. session-end: Triggered when a session ends
3. user-prompt-submit: Triggered before user prompt is submitted
4. pre-tool-use: Triggered before a tool is executed
5. post-tool-use: Triggered after a tool is executed
6. post-tool-use-failure: Triggered after a tool fails
7. pre-compact: Triggered before context window compaction
8. post-compact: Triggered after context compaction
9. stop: Triggered when agent stops
10. stop-failure: Triggered when a turn ends with an API failure
11. subagent-start: Triggered when a subagent starts
12. subagent-stop: Triggered when a subagent stops
13. task-created: Triggered when a task is created
14. task-completed: Triggered when a task is completed
15. notification: Triggered for system notifications
16. instructions-loaded: Triggered when CLAUDE.md/rules content loads
17. config-change: Triggered when Claude configuration changes
18. cwd-changed: Triggered when the working directory changes
19. file-changed: Triggered when a watched file changes
20. worktree-create: Triggered when a worktree is being created
21. worktree-remove: Triggered when a worktree is being removed
22. elicitation: Triggered when an MCP server requests user input
23. elicitation-result: Triggered after a user responds to an elicitation
24. before-model: Triggered before model inference (Gemini)
25. after-model: Triggered after model inference (Gemini)
26. permission-request: Triggered when permission is requested (Claude)
27. permission-denied: Triggered when auto mode denies a tool (Claude)

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

    POST_TOOL_USE_FAILURE = "post-tool-use-failure"
    """Triggered after a tool execution fails"""

    PRE_COMPACT = "pre-compact"
    """Triggered before context window compaction (for summary generation)"""

    POST_COMPACT = "post-compact"
    """Triggered after context compaction completes"""

    STOP = "stop"
    """Triggered when the main agent stops"""

    STOP_FAILURE = "stop-failure"
    """Triggered when a turn ends due to an API failure"""

    SUBAGENT_START = "subagent-start"
    """Triggered when a subagent (spawned via Task tool) starts"""

    SUBAGENT_STOP = "subagent-stop"
    """Triggered when a subagent (spawned via Task tool) stops"""

    TASK_CREATED = "task-created"
    """Triggered when a Claude task is created"""

    TASK_COMPLETED = "task-completed"
    """Triggered when a Claude task is completed"""

    TEAMMATE_IDLE = "teammate-idle"
    """Triggered when a Claude teammate is about to go idle"""

    NOTIFICATION = "notification"
    """Triggered for system notifications and alerts"""

    INSTRUCTIONS_LOADED = "instructions-loaded"
    """Triggered when CLAUDE.md or rule files are loaded"""

    CONFIG_CHANGE = "config-change"
    """Triggered when Claude configuration changes during a session"""

    CWD_CHANGED = "cwd-changed"
    """Triggered when the working directory changes"""

    FILE_CHANGED = "file-changed"
    """Triggered when a watched file changes"""

    WORKTREE_CREATE = "worktree-create"
    """Triggered when a worktree is being created"""

    WORKTREE_REMOVE = "worktree-remove"
    """Triggered when a worktree is being removed"""

    ELICITATION = "elicitation"
    """Triggered when an MCP server requests user input"""

    ELICITATION_RESULT = "elicitation-result"
    """Triggered after a user responds to an elicitation"""

    BEFORE_MODEL = "before-model"
    """Triggered before model inference (Gemini only)"""

    AFTER_MODEL = "after-model"
    """Triggered after model inference (Gemini only)"""

    PERMISSION_REQUEST = "permission-request"
    """Triggered when permission is requested (Claude only)"""

    PERMISSION_DENIED = "permission-denied"
    """Triggered when auto mode denies a tool (Claude only)"""


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
        populate_by_name=True,
    )


# ==================== Session Start Hook ====================


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


class PostCompactInput(HookInput):
    """Input model for post-compact hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    trigger: CompactTrigger = Field(
        default=CompactTrigger.AUTO,
        description="Compaction trigger type",
    )
    compact_summary: str | None = Field(
        default=None,
        description="Summary generated by the compaction operation",
    )
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class PostCompactOutput(HookOutput):
    """Output model for post-compact hook."""

    pass


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


class StopFailureInput(HookInput):
    """Input model for stop-failure hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    error: str = Field(..., min_length=1, description="Error type for the failed turn")
    error_details: str | None = Field(default=None, description="Additional error details")
    last_assistant_message: str | None = Field(
        default=None,
        description="Rendered API error message shown to the user",
    )
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class StopFailureOutput(HookOutput):
    """Output model for stop-failure hook."""

    pass


# ==================== Subagent Stop Hook ====================


# ==================== Subagent Start Hook ====================


class SubagentStartInput(HookInput):
    """
    Input model for subagent-start hook.

    Triggered when a subagent (spawned via Task tool) starts.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    subagent_id: str | None = Field(default=None, description="Legacy subagent identifier")
    agent_id: str | None = Field(default=None, description="Agent ID of the subagent")
    agent_type: str | None = Field(default=None, description="Agent type of the subagent")
    agent_transcript_path: str | None = Field(
        default=None,
        description="Path to the subagent's transcript file",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class SubagentStartOutput(HookOutput):
    """Output model for subagent-start hook."""

    additional_context: str | None = Field(
        default=None,
        alias="additionalContext",
        description="Context injected into the spawned subagent",
    )


class SubagentStopInput(HookInput):
    """
    Input model for subagent-stop hook.

    Triggered when a subagent (spawned via Task tool) stops.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    subagent_id: str | None = Field(default=None, description="Legacy subagent identifier")
    agent_id: str | None = Field(default=None, description="Agent ID of the subagent")
    agent_type: str | None = Field(default=None, description="Agent type of the subagent")
    agent_transcript_path: str | None = Field(
        default=None,
        description="Path to the subagent's transcript file",
    )
    reason: str | None = Field(default=None, description="Reason for stopping subagent")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class SubagentStopOutput(HookOutput):
    """Output model for subagent-stop hook."""

    pass  # Uses base HookOutput fields only


class TaskCreatedInput(HookInput):
    """Input model for task-created hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    task_id: str = Field(..., min_length=1, description="Identifier of the created task")
    task_subject: str = Field(..., min_length=1, description="Task subject/title")
    task_description: str | None = Field(default=None, description="Detailed task description")
    teammate_name: str | None = Field(default=None, description="Teammate creating the task")
    team_name: str | None = Field(default=None, description="Team name, if any")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class TaskCreatedOutput(HookOutput):
    """Output model for task-created hook."""

    pass


class TaskCompletedInput(HookInput):
    """Input model for task-completed hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    task_id: str = Field(..., min_length=1, description="Identifier of the completed task")
    task_subject: str = Field(..., min_length=1, description="Task subject/title")
    task_description: str | None = Field(default=None, description="Detailed task description")
    teammate_name: str | None = Field(default=None, description="Teammate completing the task")
    team_name: str | None = Field(default=None, description="Team name, if any")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class TaskCompletedOutput(HookOutput):
    """Output model for task-completed hook."""

    pass


class TeammateIdleInput(HookInput):
    """Input model for teammate-idle hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    teammate_name: str = Field(..., min_length=1, description="Name of the teammate")
    team_name: str = Field(..., min_length=1, description="Team name")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class TeammateIdleOutput(HookOutput):
    """Output model for teammate-idle hook."""

    pass


# ==================== Notification Hook ====================


class NotificationInput(HookInput):
    """
    Input model for notification hook.

    Triggered for system notifications and alerts.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    notification_type: str = Field(..., min_length=1, description="Type of notification")
    message: str = Field(..., min_length=1, description="Notification message")
    title: str | None = Field(default=None, description="Optional notification title")
    severity: NotificationSeverity = Field(
        default=NotificationSeverity.INFO, description="Severity level"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class NotificationOutput(HookOutput):
    """Output model for notification hook."""

    additional_context: str | None = Field(
        default=None,
        alias="additionalContext",
        description="Context injected when a notification fires",
    )


class InstructionsLoadedInput(HookInput):
    """Input model for instructions-loaded hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    file_path: str = Field(..., min_length=1, description="Loaded instruction file path")
    memory_type: str = Field(..., min_length=1, description="Instruction scope")
    load_reason: str = Field(..., min_length=1, description="Why the file was loaded")
    globs: list[str] = Field(default_factory=list, description="Optional path glob filters")
    trigger_file_path: str | None = Field(default=None, description="Triggering file path")
    parent_file_path: str | None = Field(default=None, description="Including parent file path")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class InstructionsLoadedOutput(HookOutput):
    """Output model for instructions-loaded hook."""

    pass


class ConfigChangeInput(HookInput):
    """Input model for config-change hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    source: str = Field(..., min_length=1, description="Configuration source that changed")
    file_path: str | None = Field(default=None, description="Path to the changed file")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class ConfigChangeOutput(HookOutput):
    """Output model for config-change hook."""

    pass


class CwdChangedInput(HookInput):
    """Input model for cwd-changed hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    old_cwd: str = Field(..., min_length=1, description="Previous working directory")
    new_cwd: str = Field(..., min_length=1, description="New working directory")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class CwdChangedOutput(HookOutput):
    """Output model for cwd-changed hook."""

    watch_paths: list[str] | None = Field(
        default=None,
        alias="watchPaths",
        description="Dynamic file paths to watch for FileChanged",
    )


class FileChangedInput(HookInput):
    """Input model for file-changed hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    file_path: str = Field(..., min_length=1, description="Absolute path to the changed file")
    event: str = Field(..., min_length=1, description="Filesystem event type")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class FileChangedOutput(HookOutput):
    """Output model for file-changed hook."""

    watch_paths: list[str] | None = Field(
        default=None,
        alias="watchPaths",
        description="Dynamic file paths to watch after a file change",
    )


class WorktreeCreateInput(HookInput):
    """Input model for worktree-create hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    name: str = Field(..., min_length=1, description="Worktree slug/name")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class WorktreeCreateOutput(HookOutput):
    """Output model for worktree-create hook."""

    worktree_path: str | None = Field(
        default=None,
        alias="worktreePath",
        description="Absolute path to the created worktree",
    )


class WorktreeRemoveInput(HookInput):
    """Input model for worktree-remove hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    worktree_path: str = Field(..., min_length=1, description="Absolute worktree path")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class WorktreeRemoveOutput(HookOutput):
    """Output model for worktree-remove hook."""

    pass


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


# ==================== Before Model Hook (Gemini) ====================


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


# ==================== After Model Hook (Gemini) ====================


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


# ==================== Permission Request Hook (Claude) ====================


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


# ==================== Type Mappings ====================

# Mapping of hook types to their input/output model classes
HOOK_INPUT_MODELS: dict[HookType, type[HookInput]] = {
    HookType.SESSION_START: SessionStartInput,
    HookType.SESSION_END: SessionEndInput,
    HookType.USER_PROMPT_SUBMIT: UserPromptSubmitInput,
    HookType.PRE_TOOL_USE: PreToolUseInput,
    HookType.POST_TOOL_USE: PostToolUseInput,
    HookType.POST_TOOL_USE_FAILURE: PostToolUseFailureInput,
    HookType.PRE_COMPACT: PreCompactInput,
    HookType.POST_COMPACT: PostCompactInput,
    HookType.STOP: StopInput,
    HookType.STOP_FAILURE: StopFailureInput,
    HookType.SUBAGENT_START: SubagentStartInput,
    HookType.SUBAGENT_STOP: SubagentStopInput,
    HookType.TASK_CREATED: TaskCreatedInput,
    HookType.TASK_COMPLETED: TaskCompletedInput,
    HookType.TEAMMATE_IDLE: TeammateIdleInput,
    HookType.NOTIFICATION: NotificationInput,
    HookType.INSTRUCTIONS_LOADED: InstructionsLoadedInput,
    HookType.CONFIG_CHANGE: ConfigChangeInput,
    HookType.CWD_CHANGED: CwdChangedInput,
    HookType.FILE_CHANGED: FileChangedInput,
    HookType.WORKTREE_CREATE: WorktreeCreateInput,
    HookType.WORKTREE_REMOVE: WorktreeRemoveInput,
    HookType.ELICITATION: ElicitationInput,
    HookType.ELICITATION_RESULT: ElicitationResultInput,
    HookType.BEFORE_MODEL: BeforeModelInput,
    HookType.AFTER_MODEL: AfterModelInput,
    HookType.PERMISSION_REQUEST: PermissionRequestInput,
    HookType.PERMISSION_DENIED: PermissionDeniedInput,
}

HOOK_OUTPUT_MODELS: dict[HookType, type[HookOutput]] = {
    HookType.SESSION_START: SessionStartOutput,
    HookType.SESSION_END: SessionEndOutput,
    HookType.USER_PROMPT_SUBMIT: UserPromptSubmitOutput,
    HookType.PRE_TOOL_USE: PreToolUseOutput,
    HookType.POST_TOOL_USE: PostToolUseOutput,
    HookType.POST_TOOL_USE_FAILURE: PostToolUseFailureOutput,
    HookType.PRE_COMPACT: PreCompactOutput,
    HookType.POST_COMPACT: PostCompactOutput,
    HookType.STOP: StopOutput,
    HookType.STOP_FAILURE: StopFailureOutput,
    HookType.SUBAGENT_START: SubagentStartOutput,
    HookType.SUBAGENT_STOP: SubagentStopOutput,
    HookType.TASK_CREATED: TaskCreatedOutput,
    HookType.TASK_COMPLETED: TaskCompletedOutput,
    HookType.TEAMMATE_IDLE: TeammateIdleOutput,
    HookType.NOTIFICATION: NotificationOutput,
    HookType.INSTRUCTIONS_LOADED: InstructionsLoadedOutput,
    HookType.CONFIG_CHANGE: ConfigChangeOutput,
    HookType.CWD_CHANGED: CwdChangedOutput,
    HookType.FILE_CHANGED: FileChangedOutput,
    HookType.WORKTREE_CREATE: WorktreeCreateOutput,
    HookType.WORKTREE_REMOVE: WorktreeRemoveOutput,
    HookType.ELICITATION: ElicitationOutput,
    HookType.ELICITATION_RESULT: ElicitationResultOutput,
    HookType.BEFORE_MODEL: BeforeModelOutput,
    HookType.AFTER_MODEL: AfterModelOutput,
    HookType.PERMISSION_REQUEST: PermissionRequestOutput,
    HookType.PERMISSION_DENIED: PermissionDeniedOutput,
}
