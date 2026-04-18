"""
Gobby Agents Module.

This module provides the subagent spawning system, enabling agents to spawn
independent subagents that can use any LLM provider and follow workflows.

Components:
- AgentRunner: Orchestrates agent execution with workflow integration
- Session management: Creates and links child sessions to parents
- Terminal spawning: Launches agents in separate terminal windows

Usage:
    from gobby.agents import AgentRunner, AgentConfig

    runner = AgentRunner(db, session_storage, executors)
    result = await runner.run(AgentConfig(
        prompt="Review the auth changes",
        parent_session_id="sess-123",
        project_id="proj-abc",
        machine_id="machine-1",
        source="claude",
        provider="claude",
    ))
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner as AgentRunner
    from gobby.agents.runner_models import AgentConfig as AgentConfig
    from gobby.agents.runner_models import AgentRunContext as AgentRunContext
    from gobby.agents.session import ChildSessionConfig as ChildSessionConfig
    from gobby.agents.session import ChildSessionManager as ChildSessionManager

__all__ = [
    "AgentConfig",
    "AgentRunContext",
    "AgentRunner",
    "ChildSessionConfig",
    "ChildSessionManager",
]

_EXPORTS = {
    "AgentConfig": ("gobby.agents.runner_models", "AgentConfig"),
    "AgentRunContext": ("gobby.agents.runner_models", "AgentRunContext"),
    "AgentRunner": ("gobby.agents.runner", "AgentRunner"),
    "ChildSessionConfig": ("gobby.agents.session", "ChildSessionConfig"),
    "ChildSessionManager": ("gobby.agents.session", "ChildSessionManager"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
