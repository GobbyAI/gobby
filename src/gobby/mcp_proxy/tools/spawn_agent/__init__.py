"""spawn_agent MCP tool package.

Spawns agents with configurable isolation modes:
  spawn_agent(prompt, agent="default", checkout_mode="none"|"worktree"|"clone", ...)
"""

from ._factory import create_spawn_agent_registry
from ._health import cancel_health_checks

__all__ = ["create_spawn_agent_registry", "cancel_health_checks"]
