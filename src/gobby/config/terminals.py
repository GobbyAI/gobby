"""Backend-neutral terminal configuration consumed before the host exists."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TerminalConfig(BaseModel):
    """Shared terminal settings for spawn, reaping, and REST/WS surfaces."""

    default_backend: Literal["tmux", "native"] = Field(
        default="tmux",
        description="Default TerminalRuntime backend for new Gobby-owned terminals.",
    )
    spawn_in_doubt_seconds: float = Field(
        default=150.0,
        gt=0,
        description=(
            "Age below which a pending spawn is in doubt, not dead. "
            "Defaults to the tmux init timeout (120s) plus a 30s margin."
        ),
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
