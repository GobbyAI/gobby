"""Shared context for agent MCP tool registration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
    from gobby.agents.runner import AgentRunner
    from gobby.clones.git import CloneGitManager
    from gobby.code_index.context import CodeIndexContext
    from gobby.config.app import DaemonConfig
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.hooks.hook_manager import HookManager
    from gobby.sessions.transcript_reader import TranscriptReader
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.workflows.dry_run import MCPInventoryProtocol
    from gobby.workflows.loader import WorkflowLoader
    from gobby.worktrees.git import WorktreeGitManager


@dataclass(slots=True)
class AgentsRegistryContext:
    runner: AgentRunner
    agent_run_manager: LocalAgentRunManager
    resolve_session_id: Callable[[str], str]
    get_current_session_id: Callable[[], str | None]
    get_project_context: Callable[[], dict[str, object] | None]
    session_manager: SessionManager | None = None
    task_manager: LocalTaskManager | None = None
    worktree_storage: LocalWorktreeManager | None = None
    git_manager: WorktreeGitManager | None = None
    clone_storage: LocalCloneManager | None = None
    clone_manager: CloneGitManager | None = None
    db: HubDatabase | None = None
    workflow_loader: WorkflowLoader | None = None
    mcp_inventory: MCPInventoryProtocol | None = None
    hook_manager_resolver: Callable[[], HookManager | None] | None = None
    completion_registry: CompletionEventRegistry | None = None
    lifecycle_monitor: AgentLifecycleMonitor | None = None
    daemon_config: DaemonConfig | None = None
    code_index: CodeIndexContext | None = None
    transcript_reader: TranscriptReader | None = None
