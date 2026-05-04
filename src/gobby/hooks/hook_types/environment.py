"""Environment and notification hook models.

Covers notifications, instruction loading, configuration changes, working
directory changes, watched-file changes, and worktree create/remove events.
"""

from typing import Any

from pydantic import Field

from .base import HookInput, HookOutput
from .enums import NotificationSeverity


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
