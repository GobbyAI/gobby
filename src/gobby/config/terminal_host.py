"""Host-specific settings for the gterm supervisor."""

from __future__ import annotations

from pydantic import BaseModel, Field

_MIB = 1024 * 1024


class TerminalHostConfig(BaseModel):
    """Daemon supervision settings for the gterm host process."""

    enabled: bool = Field(
        default=True,
        description="Enable gterm host supervision for native terminals.",
    )
    socket_dir: str = Field(
        default="~/.gobby",
        description="Directory for gterm sockets, pidfile, and control token.",
    )
    binary_path: str | None = Field(
        default=None,
        description="Optional absolute gterm binary path. None uses resolve_native_bin.",
    )
    health_interval_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Seconds between control ping health checks.",
    )
    shutdown_grace_seconds: float = Field(
        default=10.0,
        ge=0,
        description="Grace period for host_shutdown and process-group reaping.",
    )
    max_attachments_per_terminal: int = Field(
        default=8,
        ge=1,
        le=8,
        description="Maximum user frame attachments on one terminal.",
    )
    max_attachments_total: int = Field(
        default=128,
        ge=4,
        le=128,
        description="Attachment pool including four reserved lifecycle slots.",
    )
    max_attached_terminals: int = Field(
        default=64,
        ge=1,
        le=64,
        description="Maximum distinct terminals the host will observe.",
    )
    native_scrollback_max_lines: int = Field(
        default=10_000,
        ge=500,
        le=50_000,
        description="Oldest-first native scrollback line ceiling.",
    )
    native_scrollback_max_bytes: int = Field(
        default=8 * _MIB,
        ge=256 * 1024,
        le=32 * _MIB,
        description="Oldest-first native scrollback byte ceiling.",
    )
    tmux_attach_history_lines: int = Field(
        default=500,
        ge=1,
        le=2000,
        description="AttachHistory line cap for tmux observers (3.4).",
    )
    tmux_attach_history_max_bytes: int = Field(
        default=256 * 1024,
        ge=1024,
        le=256 * 1024,
        description="AttachHistory byte cap for tmux observers (3.4).",
    )
    tmux_poll_interval_ms: int = Field(
        default=150,
        ge=50,
        le=5_000,
        description="Host capture-poll interval for tmux observers.",
    )
    tmux_poll_backoff_ceiling_ms: int = Field(
        default=5_000,
        ge=150,
        le=30_000,
        description="Backoff ceiling for transient tmux poll failures.",
    )
