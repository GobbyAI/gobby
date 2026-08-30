"""Unified hook event models for multi-CLI session management.

This module defines the unified internal representation for hook events across
all supported CLIs (Claude Code, Droid CLI, Qwen CLI, Grok CLI, Codex CLI).
Adapters translate between CLI-specific formats and these unified types.

Design Decision: This file coexists with hook_types.py. The existing HookType enum
in hook_types.py uses Claude-specific kebab-case names (session-start, pre-tool-use)
and Pydantic models for input validation. The HookEventType enum here is the unified
internal representation. Adapters translate between them.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal


class HookEventType(str, Enum):
    """Unified hook event types across all CLI sources.

    These map to CLI-specific hook names via adapters:
    - Claude Code: kebab-case (session-start, pre-tool-use)
    - Droid CLI: PascalCase (SessionStart, PreToolUse)
    - ACP CLIs: PascalCase (SessionStart, BeforeTool)
    - Codex CLI: PascalCase hooks.json names (SessionStart, PreToolUse)
    """

    # Session lifecycle
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    SETUP = "setup"

    # Agent/turn lifecycle
    BEFORE_AGENT = "before_agent"
    AFTER_AGENT = "after_agent"
    STOP = "stop"  # Agent is about to stop/exit
    USER_PROMPT_EXPANSION = "user_prompt_expansion"

    # Tool lifecycle
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_TOOL_SELECTION = "before_tool_selection"
    POST_TOOL_BATCH = "post_tool_batch"

    # Model lifecycle
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"

    # Context management
    PRE_COMPACT = "pre_compact"  # Claude/Codex: PreCompact, ACP CLIs: PreCompress
    POST_COMPACT = "post_compact"  # Claude/Codex: PostCompact

    # Subagent lifecycle
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"

    # Permissions & notifications
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DENIED = "permission_denied"
    NOTIFICATION = "notification"
    MESSAGE_DISPLAY = "message_display"
    DIRECTORY_ADDED = "directory_added"

    # Provider-specific lifecycle and observability events
    STOP_FAILURE = "stop_failure"
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    TEAMMATE_IDLE = "teammate_idle"
    INSTRUCTIONS_LOADED = "instructions_loaded"
    CONFIG_CHANGE = "config_change"
    CWD_CHANGED = "cwd_changed"
    FILE_CHANGED = "file_changed"
    WORKTREE_CREATE = "worktree_create"
    WORKTREE_REMOVE = "worktree_remove"
    ELICITATION = "elicitation"
    ELICITATION_RESULT = "elicitation_result"


class SessionSource(str, Enum):
    """Identifies which CLI originated the session."""

    AGY = "agy"
    CLAUDE = "claude"
    DROID = "droid"
    GROK = "grok"
    QWEN = "qwen"
    CODEX = "codex"
    PIPELINE = "pipeline"
    UNKNOWN = "unknown"


def parse_session_source(
    value: SessionSource | str | None,
    *,
    default: SessionSource = SessionSource.UNKNOWN,
) -> SessionSource:
    """Return a known session source, falling back for stale or unknown values."""
    if isinstance(value, SessionSource):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return SessionSource(value.strip().lower())
    except ValueError:
        return default


@dataclass
class HookEvent:
    """Unified hook event from any CLI source.

    This dataclass represents a normalized hook event that can originate from
    any supported CLI. Adapters are responsible for translating CLI-specific
    payloads into this format.

    Attributes:
        event_type: The type of hook event (from HookEventType enum).
        session_id: External session identifier (external_id for Claude, thread_id for Codex).
        source: Which CLI originated this event.
        timestamp: When the event occurred.
        data: Event-specific payload in native format (adapter passes through).

        machine_id: Unique identifier for the machine (populated by adapter or manager).
        cwd: Current working directory for the session.

        user_id: Platform user ID (populated by HookManager after session lookup).
        project_id: Platform project ID (populated by HookManager).
        workflow_id: Future: ID of workflow evaluating this event.
        metadata: Extensible key-value store for adapter-specific data.
    """

    # Core required fields
    event_type: HookEventType
    session_id: str  # external_id / thread_id (external ID)
    source: SessionSource
    timestamp: datetime
    data: dict[str, Any]  # Event-specific payload (native format)

    # Context (populated by adapter or manager)
    machine_id: str | None = None
    cwd: str | None = None

    # Future extensibility
    user_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResponse:
    """Unified response returned to CLI.

    This dataclass represents the response that will be translated back to
    CLI-specific format by the adapter.

    Attributes:
        decision: Whether to allow, deny, or ask the user about the action.
        context: Text to inject into the agent's context (AI-only).
        system_message: User-visible message to display (e.g., handoff notification).
        reason: Explanation for the decision (useful for denials).

        modify_args: Future: Dict of argument modifications for the action.
        trigger_action: Future: Action to trigger in the CLI.
        metadata: Extensible key-value store for adapter-specific data.
    """

    decision: Literal["allow", "deny", "ask", "block", "modify"] = "allow"
    context: str | None = None  # Inject into agent context (AI-only)
    system_message: str | None = None  # User-visible message (e.g., handoff notification)
    reason: str | None = None  # Explanation for decision
    display_content: str | None = None  # Replacement MessageDisplay delta

    # Input rewriting (PreToolUse / PermissionRequest)
    modified_input: dict[str, Any] | None = None
    auto_approve: bool = False
    permission_decision: Literal["allow", "deny"] | None = None
    updated_permissions: list[dict[str, Any]] | None = None

    # Event-specific Claude outputs
    retry: bool = False
    watch_paths: list[str] | None = None
    worktree_path: str | None = None
    elicitation_action: Literal["accept", "decline", "cancel"] | None = None
    elicitation_content: dict[str, Any] | None = None
    elicitation_error: str | None = None

    # Future extensibility
    modify_args: dict[str, Any] | None = None
    trigger_action: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# Event type mapping table for documentation (see plan-multi-cli.md section 1.2)
# This is informational - actual mappings are in adapters
EVENT_TYPE_CLI_SUPPORT: dict[HookEventType, dict[str, str | None]] = {
    HookEventType.SETUP: {"claude": "Setup", "qwen": None, "codex": None},
    HookEventType.SESSION_START: {
        "claude": "SessionStart",
        "qwen": "SessionStart",
        "codex": "SessionStart",
    },
    HookEventType.SESSION_END: {
        "claude": "SessionEnd",
        "qwen": "SessionEnd",
        "codex": "SessionEnd",
    },
    HookEventType.BEFORE_AGENT: {
        "claude": "UserPromptSubmit",
        "qwen": "UserPromptSubmit",
        "codex": "UserPromptSubmit",
    },
    HookEventType.USER_PROMPT_EXPANSION: {
        "claude": "UserPromptExpansion",
        "qwen": None,
        "codex": None,
    },
    HookEventType.AFTER_AGENT: {
        "claude": "Stop",
        "qwen": "Stop",
        "codex": None,
    },
    HookEventType.STOP: {
        "claude": "Stop",
        "qwen": "Stop",
        "codex": "Stop",
    },
    HookEventType.BEFORE_TOOL: {
        "claude": "PreToolUse",
        "qwen": "PreToolUse",
        "codex": "PreToolUse",
    },
    HookEventType.AFTER_TOOL: {
        "claude": "PostToolUse",
        "qwen": "PostToolUse",
        "codex": "PostToolUse",
    },
    HookEventType.BEFORE_TOOL_SELECTION: {
        "claude": None,
        "qwen": None,
        "codex": None,
    },
    HookEventType.POST_TOOL_BATCH: {
        "claude": "PostToolBatch",
        "qwen": None,
        "codex": None,
    },
    HookEventType.BEFORE_MODEL: {
        "claude": None,
        "qwen": None,
        "codex": None,
    },
    HookEventType.AFTER_MODEL: {
        "claude": None,
        "qwen": None,
        "codex": None,
    },
    HookEventType.PRE_COMPACT: {
        "claude": "PreCompact",
        "qwen": "PreCompact",
        "codex": "PreCompact",
    },
    HookEventType.POST_COMPACT: {
        "claude": "PostCompact",
        "qwen": "PostCompact",
        "codex": "PostCompact",
    },
    HookEventType.SUBAGENT_START: {
        "claude": "SubagentStart",
        "qwen": "SubagentStart",
        "codex": "SubagentStart",
    },
    HookEventType.SUBAGENT_STOP: {
        "claude": "SubagentStop",
        "qwen": "SubagentStop",
        "codex": "SubagentStop",
    },
    HookEventType.PERMISSION_REQUEST: {
        "claude": "PermissionRequest",
        "qwen": "PermissionRequest",
        "codex": "PermissionRequest",
    },
    HookEventType.PERMISSION_DENIED: {
        "claude": "PermissionDenied",
        "qwen": None,
        "codex": None,
    },
    HookEventType.NOTIFICATION: {
        "claude": "Notification",
        "qwen": "Notification",
        "codex": None,
    },
    HookEventType.MESSAGE_DISPLAY: {
        "claude": "MessageDisplay",
        "qwen": None,
        "codex": None,
    },
    HookEventType.DIRECTORY_ADDED: {
        "claude": "DirectoryAdded",
        "qwen": None,
        "codex": None,
    },
    HookEventType.STOP_FAILURE: {
        "claude": "StopFailure",
        "qwen": "StopFailure",
        "codex": None,
    },
    HookEventType.TASK_CREATED: {
        "claude": "TaskCreated",
        "qwen": "TodoCreated",
        "codex": None,
    },
    HookEventType.TASK_COMPLETED: {
        "claude": "TaskCompleted",
        "qwen": "TodoCompleted",
        "codex": None,
    },
    HookEventType.TEAMMATE_IDLE: {
        "claude": "TeammateIdle",
        "qwen": None,
        "codex": None,
    },
    HookEventType.INSTRUCTIONS_LOADED: {
        "claude": "InstructionsLoaded",
        "qwen": None,
        "codex": None,
    },
    HookEventType.CONFIG_CHANGE: {
        "claude": "ConfigChange",
        "qwen": None,
        "codex": None,
    },
    HookEventType.CWD_CHANGED: {
        "claude": "CwdChanged",
        "qwen": None,
        "codex": None,
    },
    HookEventType.FILE_CHANGED: {
        "claude": "FileChanged",
        "qwen": None,
        "codex": None,
    },
    HookEventType.WORKTREE_CREATE: {
        "claude": "WorktreeCreate",
        "qwen": None,
        "codex": None,
    },
    HookEventType.WORKTREE_REMOVE: {
        "claude": "WorktreeRemove",
        "qwen": None,
        "codex": None,
    },
    HookEventType.ELICITATION: {
        "claude": "Elicitation",
        "qwen": None,
        "codex": None,
    },
    HookEventType.ELICITATION_RESULT: {
        "claude": "ElicitationResult",
        "qwen": None,
        "codex": None,
    },
}

for _support in EVENT_TYPE_CLI_SUPPORT.values():
    _support.setdefault("droid", None)
    _support.setdefault("agy", None)
    _support.setdefault("grok", None)

EVENT_TYPE_CLI_SUPPORT[HookEventType.SESSION_START]["droid"] = "SessionStart"
EVENT_TYPE_CLI_SUPPORT[HookEventType.SESSION_END]["droid"] = "SessionEnd"
EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_AGENT]["droid"] = "UserPromptSubmit"
EVENT_TYPE_CLI_SUPPORT[HookEventType.NOTIFICATION]["droid"] = "Notification"
EVENT_TYPE_CLI_SUPPORT[HookEventType.STOP]["droid"] = "Stop"
EVENT_TYPE_CLI_SUPPORT[HookEventType.SUBAGENT_STOP]["droid"] = "SubagentStop"
EVENT_TYPE_CLI_SUPPORT[HookEventType.PRE_COMPACT]["droid"] = "PreCompact"
EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_TOOL]["droid"] = "PreToolUse"
EVENT_TYPE_CLI_SUPPORT[HookEventType.AFTER_TOOL]["droid"] = "PostToolUse"

EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_AGENT]["agy"] = "PreInvocation"
EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_TOOL]["agy"] = "PreToolUse"
EVENT_TYPE_CLI_SUPPORT[HookEventType.AFTER_TOOL]["agy"] = "PostToolUse"
EVENT_TYPE_CLI_SUPPORT[HookEventType.AFTER_AGENT]["agy"] = "PostInvocation"
EVENT_TYPE_CLI_SUPPORT[HookEventType.STOP]["agy"] = "Stop"

EVENT_TYPE_CLI_SUPPORT[HookEventType.SESSION_START]["grok"] = "session_start"
EVENT_TYPE_CLI_SUPPORT[HookEventType.SESSION_END]["grok"] = "session_end"
EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_AGENT]["grok"] = "user_prompt_submit"
EVENT_TYPE_CLI_SUPPORT[HookEventType.STOP]["grok"] = "stop"
EVENT_TYPE_CLI_SUPPORT[HookEventType.BEFORE_TOOL]["grok"] = "pre_tool_use"
EVENT_TYPE_CLI_SUPPORT[HookEventType.AFTER_TOOL]["grok"] = "post_tool_use"
EVENT_TYPE_CLI_SUPPORT[HookEventType.PRE_COMPACT]["grok"] = "pre_compact"
EVENT_TYPE_CLI_SUPPORT[HookEventType.POST_COMPACT]["grok"] = "post_compact"
EVENT_TYPE_CLI_SUPPORT[HookEventType.NOTIFICATION]["grok"] = "notification"
EVENT_TYPE_CLI_SUPPORT[HookEventType.PERMISSION_DENIED]["grok"] = "permission_denied"
EVENT_TYPE_CLI_SUPPORT[HookEventType.STOP_FAILURE]["grok"] = "stop_failure"
EVENT_TYPE_CLI_SUPPORT[HookEventType.SUBAGENT_START]["grok"] = "subagent_start"
EVENT_TYPE_CLI_SUPPORT[HookEventType.SUBAGENT_STOP]["grok"] = "subagent_stop"
