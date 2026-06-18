"""Factory for the internal agent MCP tool registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.agents_lifecycle_tools import register_agent_lifecycle_tools
from gobby.mcp_proxy.tools.agents_query_tools import register_agent_query_tools
from gobby.mcp_proxy.tools.agents_spawn_tools import register_agent_spawn_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.agents import LocalAgentRunManager

if TYPE_CHECKING:
    from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
    from gobby.agents.runner import AgentRunner


def create_agents_registry(
    runner: AgentRunner,
    session_manager: Any | None = None,
    # spawn_agent dependencies
    task_manager: Any | None = None,
    worktree_storage: Any | None = None,
    git_manager: Any | None = None,
    clone_storage: Any | None = None,
    clone_manager: Any | None = None,
    # For mode=self (workflow activation on caller session)
    db: Any | None = None,
    # For firing synthetic stop events on agent kill
    hook_manager_resolver: Any | None = None,
    completion_registry: Any | None = None,
    lifecycle_monitor: AgentLifecycleMonitor | None = None,
    # Legacy parameter — ignored, kept for caller compatibility during migration
    running_registry: Any | None = None,
    daemon_config: Any | None = None,
    code_index: Any | None = None,
    transcript_reader: Any | None = None,
) -> InternalToolRegistry:
    """
    Create an agent tool registry with all agent-related tools.

    Args:
        runner: AgentRunner instance for executing agents.
        session_manager: Optional SessionManager for resolving session references.
        task_manager: Task manager for spawn_agent task resolution.
        worktree_storage: Worktree storage for spawn_agent isolation.
        git_manager: Git manager for spawn_agent isolation.
        clone_storage: Clone storage for spawn_agent isolation.
        clone_manager: Clone git manager for spawn_agent isolation.
        db: Database instance for agent definition lookups.
        completion_registry: CompletionEventRegistry for auto-subscribing parent sessions.

    Returns:
        InternalToolRegistry with all agent tools registered.
    """
    from gobby.utils.project_context import get_project_context
    from gobby.utils.session_context import get_current_session_id, resolve_session_ref

    agent_run_manager = LocalAgentRunManager(db) if db else runner.run_storage

    def _resolve_session_id(ref: str) -> str:
        return resolve_session_ref(session_manager, ref)

    registry = InternalToolRegistry(
        name="gobby-agents",
        description="Agent spawning - start, monitor, and manage subagents",
    )
    ctx = AgentsRegistryContext(
        runner=runner,
        session_manager=session_manager,
        task_manager=task_manager,
        worktree_storage=worktree_storage,
        git_manager=git_manager,
        clone_storage=clone_storage,
        clone_manager=clone_manager,
        db=db,
        hook_manager_resolver=hook_manager_resolver,
        completion_registry=completion_registry,
        lifecycle_monitor=lifecycle_monitor,
        daemon_config=daemon_config,
        code_index=code_index,
        transcript_reader=transcript_reader,
        agent_run_manager=agent_run_manager,
        resolve_session_id=_resolve_session_id,
        get_current_session_id=get_current_session_id,
        get_project_context=get_project_context,
    )

    register_agent_query_tools(registry, ctx)
    register_agent_lifecycle_tools(registry, ctx)
    register_agent_spawn_tools(registry, ctx)
    return registry
