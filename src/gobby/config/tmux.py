"""Configuration for the tmux agent spawning module."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TmuxConfig(BaseModel):
    """Configuration for tmux-based agent spawning.

    Controls how Gobby creates and manages tmux sessions for agents.
    All sessions use ``-L <socket_name>`` to isolate from the user's
    personal tmux server.
    """

    enabled: bool = Field(
        default=True,
        description="Enable tmux as first-class agent spawning backend.",
    )
    command: str = Field(
        default="tmux",
        description="Path or name of the tmux binary.",
    )
    socket_name: str = Field(
        default="gobby",
        description="Isolated tmux socket name (passed as -L <socket_name>).",
    )
    socket_path: str | None = Field(
        default=None,
        description=(
            "Exact tmux socket path (passed as -S <socket_path>). "
            "When set, this takes precedence over socket_name."
        ),
    )
    config_file: str | None = Field(
        default=None,
        description="Optional tmux config file (passed as -f <path>).",
    )
    session_prefix: str = Field(
        default="gobby",
        description="Prefix for auto-generated session names.",
    )
    history_limit: int = Field(
        default=10000,
        ge=100,
        description="Scrollback buffer size for spawned sessions.",
    )
    wsl_distribution: str | None = Field(
        default=None,
        description="WSL distribution for Windows (e.g., 'Ubuntu'). None uses default.",
    )
    idle_check_enabled: bool = Field(
        default=True,
        description="Enable idle agent detection and auto-reprompting.",
    )
    idle_timeout_seconds: int = Field(
        default=60,
        ge=10,
        description="Seconds an agent must be idle before triggering a reprompt.",
    )
    max_reprompt_attempts: int = Field(
        default=3,
        ge=1,
        description="Maximum reprompt attempts before failing an idle agent.",
    )
    init_timeout_seconds: int = Field(
        default=120,
        ge=30,
        description="Seconds before an uninitialized agent is killed as a provider failure.",
    )
    init_activity_grace_seconds: float = Field(
        default=5.0,
        gt=0,
        description=(
            "Seconds of session activity allowed during initialization before the agent is "
            "considered initialized."
        ),
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
