"""Factory function for creating the task ops tool registry.

Orchestrates the creation of cold-path task tool sub-registries and merges
them into a unified gobby-tasks-ops registry. This server contains tools
used by pipelines and occasional manual operations, not daily-driver
session work.

Tools included:
- Expansion (8): start/get/latest/resume/cancel/validate run, QA result, plan validation
- Front half (1): front_half_tick
- Affected files (4): set, get, find_overlaps, wire_from_run
- GitHub (2): import_github_issues, link_task_to_github_issue
- Reindex (1): reindex_tasks
"""

from typing import TYPE_CHECKING

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_github import create_github_registry
from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._expansion import create_expansion_registry
from gobby.mcp_proxy.tools.tasks._front_half import create_front_half_registry
from gobby.mcp_proxy.tools.tasks._search import create_reindex_registry
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager
from gobby.tasks.validation import TaskValidator

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.llm.service import LLMService


def create_task_ops_registry(
    task_manager: LocalTaskManager,
    sync_manager: TaskSyncManager,
    task_validator: TaskValidator | None = None,
    config: "DaemonConfig | None" = None,
    llm_service: "LLMService | None" = None,
    completion_registry: "CompletionEventRegistry | None" = None,
) -> InternalToolRegistry:
    """Create a task ops tool registry with cold-path task tools.

    Args:
        task_manager: LocalTaskManager instance
        sync_manager: TaskSyncManager instance
        task_validator: TaskValidator instance (optional)
        config: DaemonConfig instance (optional)

    Returns:
        InternalToolRegistry with ops task tools registered
    """
    # Create own RegistryContext (lightweight, shares the same manager objects)
    ctx = RegistryContext(
        task_manager=task_manager,
        sync_manager=sync_manager,
        task_validator=task_validator,
        config=config,
        llm_service=llm_service,
        completion_registry=completion_registry,
    )

    registry = InternalToolRegistry(
        name="gobby-tasks-ops",
        description="Task operations - expansion, front-half orchestration, affected files, GitHub, reindex",
    )

    # Merge expansion tools
    registry.merge_from(create_expansion_registry(ctx))

    # Merge front-half conductor tools
    registry.merge_from(create_front_half_registry(ctx))

    # Merge ops affected files tools (set, get, find_overlaps, wire_from_run)
    registry.merge_from(create_ops_affected_files_registry(ctx))

    # Merge GitHub integration tools (2 tools)
    registry.merge_from(create_github_registry(ctx))

    # Merge reindex tool (1 tool)
    registry.merge_from(create_reindex_registry(ctx))

    return registry
