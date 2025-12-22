"""Unified hook event models for multi-CLI session management.

This module defines the unified internal representation for hook events across
all supported CLIs (Claude Code, Gemini CLI, Codex CLI). Adapters translate
between CLI-specific formats and these unified types.

Design Decision: This file coexists with hook_types.py. The existing HookType enum
in hook_types.py uses Claude-specific kebab-case names (session-start, pre-tool-use)
and Pydantic models for input validation. The HookEventType enum here is the unified
internal representation. Adapters translate between them.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class HookEventType(str, Enum):
    """Unified hook event types across all CLI sources.

    These map to CLI-specific hook names via adapters:
    - Claude Code: kebab-case (session-start, pre-tool-use)
    - Gemini CLI: PascalCase (SessionStart, BeforeTool)
    - Codex CLI: JSON-RPC methods (thread/started, item/completed)
    """

    # Session lifecycle
    SESSION_START = "session_start"
    SESSION_END = "session_end"

    # Agent/turn lifecycle
    BEFORE_AGENT = "before_agent"
    AFTER_AGENT = "after_agent"

    # Tool lifecycle
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_TOOL_SELECTION = "before_tool_selection"  # Gemini only

    # Model lifecycle (Gemini only)
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"

    # Context management
    PRE_COMPACT = "pre_compact"  # Claude: PreCompact, Gemini: PreCompress

    # Subagent lifecycle (Claude Code only)
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"

    # Permissions & notifications
    PERMISSION_REQUEST = "permission_request"  # Claude Code only
    NOTIFICATION = "notification"


class SessionSource(str, Enum):
    """Identifies which CLI originated the session."""

    CLAUDE = "claude"  # Claude Code CLI
    GEMINI = "gemini"
    CODEX = "codex"
    CLAUDE_SDK = "claude_sdk"


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
    data: dict  # Event-specific payload (native format)

    # Context (populated by adapter or manager)
    machine_id: str | None = None
    cwd: str | None = None

    # Future extensibility
    user_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    workflow_id: str | None = None
    metadata: dict = field(default_factory=dict)


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

    # Future extensibility
    modify_args: dict | None = None
    trigger_action: str | None = None
    metadata: dict = field(default_factory=dict)


# Event type mapping table for documentation (see plan-multi-cli.md section 1.2)
# This is informational - actual mappings are in adapters
EVENT_TYPE_CLI_SUPPORT: dict[HookEventType, dict[str, str | None]] = {
    HookEventType.SESSION_START: {
        "claude": "SessionStart",
        "gemini": "SessionStart",
        "codex": "thread/started",
    },
    HookEventType.SESSION_END: {
        "claude": "SessionEnd",
        "gemini": "SessionEnd",
        "codex": "thread/archive",
    },
    HookEventType.BEFORE_AGENT: {
        "claude": "UserPromptSubmit",
        "gemini": "BeforeAgent",
        "codex": "turn/started",
    },
    HookEventType.AFTER_AGENT: {
        "claude": "Stop",
        "gemini": "AfterAgent",
        "codex": "turn/completed",
    },
    HookEventType.BEFORE_TOOL: {
        "claude": "PreToolUse",
        "gemini": "BeforeTool",
        "codex": "requestApproval",
    },
    HookEventType.AFTER_TOOL: {
        "claude": "PostToolUse",
        "gemini": "AfterTool",
        "codex": "item/completed",
    },
    HookEventType.BEFORE_TOOL_SELECTION: {
        "claude": None,
        "gemini": "BeforeToolSelection",
        "codex": None,
    },
    HookEventType.BEFORE_MODEL: {
        "claude": None,
        "gemini": "BeforeModel",
        "codex": None,
    },
    HookEventType.AFTER_MODEL: {
        "claude": None,
        "gemini": "AfterModel",
        "codex": None,
    },
    HookEventType.PRE_COMPACT: {
        "claude": "PreCompact",
        "gemini": "PreCompress",
        "codex": None,
    },
    HookEventType.SUBAGENT_START: {
        "claude": "SubagentStart",
        "gemini": None,
        "codex": None,
    },
    HookEventType.SUBAGENT_STOP: {
        "claude": "SubagentStop",
        "gemini": None,
        "codex": None,
    },
    HookEventType.PERMISSION_REQUEST: {
        "claude": "PermissionRequest",
        "gemini": None,
        "codex": None,
    },
    HookEventType.NOTIFICATION: {
        "claude": "Notification",
        "gemini": "Notification",
        "codex": None,
    },
}
