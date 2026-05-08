"""Enums shared across all hook input/output models."""

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
