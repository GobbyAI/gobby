"""Configuration for hand-started tmux panes and agent terminal monitoring."""

from __future__ import annotations

from pydantic import BaseModel, Field

# The one source of the attach-history default the terminal host and the
# proxy relay share. 500 matches herdr's default bound. The cost model behind it is measured by
# web/tests/history-perf.spec.ts and recorded on the field below.
ATTACH_HISTORY_LINES = 500


class TmuxConfig(BaseModel):
    """How Gobby monitors agent terminals and restores attach history.

    Gobby spawns no tmux sessions and runs no tmux server (#22856); a
    hand-started pane is addressed by the socket it recorded from ``$TMUX``.
    """

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
