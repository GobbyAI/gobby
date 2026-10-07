"""Backend-neutral terminal configuration consumed before the host exists."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TerminalConfig(BaseModel):
    """Shared terminal settings for spawn, reaping, and REST/WS surfaces.

    No setting selects a backend: native gterm is always attempted first.
    """

    model_config = ConfigDict(extra="forbid")

    spawn_in_doubt_seconds: float = Field(
        default=150.0,
        gt=0,
        description="Age below which a pending spawn is in doubt, not dead.",
    )
    hook_write_timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=30.0,
        description="How long a hook thread waits for a coordinator write to dispatch.",
    )
    hook_write_shutdown_timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=30.0,
        description="How long shutdown drains in-flight TerminalEffectBridge tasks.",
    )
    stop_host_on_shutdown: bool = Field(
        default=False,
        description=(
            "Drain the gterm host (and every native terminal it owns) whenever the "
            "daemon stops or restarts. Off by default: the host outlives the daemon "
            "and is adopted again on the next start; `gobby stop --terminals` drains "
            "it for one shutdown."
        ),
    )
