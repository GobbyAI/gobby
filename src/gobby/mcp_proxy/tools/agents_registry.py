"""Factory for the internal agent MCP tool registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.agents_lifecycle_tools import register_agent_lifecycle_tools
from gobby.mcp_proxy.tools.agents_query_tools import register_agent_query_tools
from gobby.mcp_proxy.tools.agents_spawn_tools import register_agent_spawn_tools
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.agents import LocalAgentRunManager

if TYPE_CHECKING:
    from gobby.agents.detection.registry import DetectionManifestRegistry
    from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
    from gobby.agents.runner import AgentRunner
    from gobby.clones.git import CloneGitManager
    from gobby.code_index.context import CodeIndexContext
    from gobby.config.app import DaemonConfig
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.sessions.transcript_reader import TranscriptReader
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.workflows.dry_run import MCPInventoryProtocol
    from gobby.workflows.pipeline_loader import PipelineLoader
    from gobby.worktrees.git import WorktreeGitManager


def create_agents_registry(
    runner: AgentRunner,
    session_manager: SessionManager | None = None,
    # spawn_agent dependencies
    task_manager: LocalTaskManager | None = None,
    worktree_storage: LocalWorktreeManager | None = None,
    git_manager: WorktreeGitManager | None = None,
    clone_storage: LocalCloneManager | None = None,
    clone_manager: CloneGitManager | None = None,
    # For mode=self (workflow activation on caller session)
    db: HubDatabase | None = None,
    workflow_loader: PipelineLoader | None = None,
    mcp_inventory: MCPInventoryProtocol | None = None,
    completion_registry: CompletionEventRegistry | None = None,
    lifecycle_monitor: AgentLifecycleMonitor | None = None,
    startup_config: DaemonConfig | None = None,
    config_resolver: Callable[[], DaemonConfig | None] | None = None,
    code_index: CodeIndexContext | None = None,
    transcript_reader: TranscriptReader | None = None,
    detection_registry: DetectionManifestRegistry | None = None,
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
        workflow_loader: Workflow loader used by spawn dry-run validation.
        mcp_inventory: Combined internal and external MCP tool inventory.
        completion_registry: CompletionEventRegistry for auto-subscribing parent sessions.
        lifecycle_monitor: Agent lifecycle monitor for termination cleanup.
        startup_config: DaemonConfig fallback before runtime readiness.
        config_resolver: per-operation current DaemonConfig resolver.
        code_index: Code index context exposed to spawn_agent.
        transcript_reader: Transcript reader for agent query payloads.

    Returns:
        InternalToolRegistry with all agent tools registered.
    """
    from gobby.utils.project_context import get_project_context
    from gobby.utils.session_context import (
        get_current_agent_run_id,
        get_current_session_id,
        resolve_session_ref,
    )

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
        workflow_loader=workflow_loader,
        mcp_inventory=mcp_inventory,
        completion_registry=completion_registry,
        lifecycle_monitor=lifecycle_monitor,
        startup_config=startup_config,
        config_resolver=config_resolver,
        code_index=code_index,
        transcript_reader=transcript_reader,
        detection_registry=detection_registry,
        agent_run_manager=agent_run_manager,
        resolve_session_id=_resolve_session_id,
        get_current_session_id=get_current_session_id,
        get_current_agent_run_id=get_current_agent_run_id,
        get_project_context=get_project_context,
    )

    register_agent_query_tools(registry, ctx)
    register_agent_lifecycle_tools(registry, ctx)
    register_agent_spawn_tools(registry, ctx)
    return registry
