"""Shared context for agent MCP tool registration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
    from gobby.agents.runner import AgentRunner


@dataclass(slots=True)
class AgentsRegistryContext:
    runner: AgentRunner
    session_manager: Any | None
    task_manager: Any | None
    worktree_storage: Any | None
    git_manager: Any | None
    clone_storage: Any | None
    clone_manager: Any | None
    db: Any | None
    hook_manager_resolver: Any | None
    completion_registry: Any | None
    lifecycle_monitor: AgentLifecycleMonitor | None
    daemon_config: Any | None
    code_index: Any | None
    transcript_reader: Any | None
    agent_run_manager: Any
    resolve_session_id: Callable[[str], str]
    get_current_session_id: Callable[[], str | None]
    get_project_context: Callable[[], dict[str, Any] | None]
