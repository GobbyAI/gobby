"""Types for session-start event handling."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentActivationResult:
    """Result of activating the default agent for a session."""

    context: str | None  # Legacy activation metadata; not injected at SessionStart.
    agent_name: str
    description: str | None
    role: str | None
    goal: str | None
    rules_count: int
    skills_count: int
    variables_count: int
    injected_skill_names: list[str]  # skills with format "full" or "content"
