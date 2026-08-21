"""Host-specific settings for the gterm supervisor."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
