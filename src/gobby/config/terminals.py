"""Backend-neutral terminal configuration consumed before the host exists."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

ATTACH_HISTORY_LINES = 500


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

    attach_history_lines: int = Field(
        default=ATTACH_HISTORY_LINES,
        ge=0,
        le=2000,
        description=(
            "Scrollback lines restored to the web terminal on attach; 0 disables the "
            "restore. Render cost is linear in lines on the primary VT core "
            "(~0.26 ms/line on a desktop, ~0.9 ms/line at the pinned 4x mobile "
            "throttle), and the built-in fallback core retains at most 1000 rows, "
            "so the ceiling is where a larger window stops being deliverable."
        ),
    )
    idle_check_enabled: bool = Field(
        default=True,
        description="Enable idle agent detection and auto-reprompting.",
    )
    idle_timeout_seconds: int = Field(
        default=60,
        ge=10,
        description="Seconds before an idle health check considers the session stale.",
    )
    idle_reprompt_delay_seconds: int = Field(
        default=300,
        ge=60,
        description="Seconds an agent must be inactive before sending a semantic idle reprompt.",
    )
    max_reprompt_attempts: int = Field(
        default=3,
        ge=1,
        description="Maximum reprompt attempts before failing an idle agent.",
    )
    reasoning_watchdog_interrupt_enabled: bool = Field(
        default=True,
        description=(
            "Interrupt Codex reasoning turns that exceed the idle timeout without workflow progress."
        ),
    )
    reasoning_watchdog_settle_seconds: float = Field(
        default=0.2,
        ge=0.0,
        description="Seconds to wait after Ctrl-C before sending the watchdog continuation prompt.",
    )
    init_timeout_seconds: int = Field(
        default=120,
        ge=30,
        description="Seconds before an uninitialized agent is killed as a provider failure.",
    )
    auto_enter_approval_prompts: bool = Field(
        default=True,
        description="Automatically send Enter for spawned-agent approval prompts.",
    )
    auto_enter_agent_terminals: bool = Field(
        default=True,
        description="Periodically send Enter to active spawned-agent terminal panes.",
    )
    auto_enter_agent_interval_seconds: int = Field(
        default=30,
        ge=1,
        description="Minimum seconds between periodic Enter keypresses per agent terminal.",
    )
