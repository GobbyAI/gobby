"""Enums shared across all hook input/output models."""

from __future__ import annotations

from enum import Enum


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

    SETUP = "setup"
    """Triggered when Claude Code starts or runs repository maintenance"""

    USER_PROMPT_SUBMIT = "user-prompt-submit"
    """Triggered before user prompt is submitted for validation/filtering"""

    USER_PROMPT_EXPANSION = "user-prompt-expansion"
    """Triggered when a slash command or skill expands into a prompt"""

    PRE_TOOL_USE = "pre-tool-use"
    """Triggered before a tool is executed (for context injection)"""

    POST_TOOL_USE = "post-tool-use"
    """Triggered after a tool is executed (for memory saving)"""

    POST_TOOL_USE_FAILURE = "post-tool-use-failure"
    """Triggered after a tool execution fails"""

    POST_TOOL_BATCH = "post-tool-batch"
    """Triggered after a batch of tool calls completes"""

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

    MESSAGE_DISPLAY = "message-display"
    """Triggered while Claude renders an assistant message delta"""

    DIRECTORY_ADDED = "directory-added"
    """Triggered after a working directory is added to the session"""

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
    """Triggered before model inference"""

    AFTER_MODEL = "after-model"
    """Triggered after model inference"""

    PERMISSION_REQUEST = "permission-request"
    """Triggered when permission is requested (Claude only)"""

    PERMISSION_DENIED = "permission-denied"
    """Triggered when YOLO mode denies a tool (Claude only)"""


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

    @classmethod
    def _missing_(cls, value: object) -> SessionStartSource | None:
        """Map provider aliases to canonical members.

        Grok emits SessionStart with ``source: "new"`` for a cold start and
        after ``/clear``, and ``source: "load"`` after compact/resume. Accept
        those aliases so Pydantic broadcast validation succeeds. Identity
        resolution still promotes a compact restart to ``compact`` when the
        row is marked, and a ``/clear`` successor to ``clear`` when a unique
        unconsumed clear attempt matches the terminal identity.
        """
        if not isinstance(value, str):
            return None
        alias = value.strip().lower()
        if alias == "new":
            return cls.STARTUP
        if alias == "load":
            return cls.RESUME
        return None


class SessionEndReason(str, Enum):
    """Reason for session end events."""

    CLEAR = "clear"
    """User cleared the session"""

    RESUME = "resume"
    """Session ended because another session resumed or replaced it"""

    COMPACT = "compact"
    """Session ended because context was compacted for handoff"""

    IDLE = "idle"
    """Durable session runtime was evicted after becoming idle"""

    LOGOUT = "logout"
    """User logged out"""

    PROMPT_INPUT_EXIT = "prompt_input_exit"
    """User exited from prompt input"""

    EXIT = "exit"
    """Session exited normally"""

    OTHER = "other"
    """Other/unspecified reason"""

    @classmethod
    def _missing_(cls, value: object) -> SessionEndReason | None:
        """Map provider aliases to canonical members.

        Grok 1.0.3 emits SessionEnd with ``reason: "shutdown"``. Accept that
        alias as ``exit`` so Pydantic broadcast validation succeeds.
        """
        if isinstance(value, str) and value.strip().lower() == "shutdown":
            return cls.EXIT
        return None


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
