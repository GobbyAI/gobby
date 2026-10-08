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
