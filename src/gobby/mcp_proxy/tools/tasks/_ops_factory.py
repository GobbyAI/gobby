"""Factory function for creating the task ops tool registry.

Orchestrates the creation of cold-path task tool sub-registries and merges
them into a unified gobby-tasks-ops registry. This server contains tools
used by pipelines and occasional manual operations, not daily-driver
session work.

Tools included:
- Expansion: start/get/latest/resume/cancel/validate run, QA result, QA coverage, plan validation
- Front half (1): front_half_tick
- Affected files (4): set, get, find_overlaps, wire_from_run
- Artifacts (5): set/get artifact pointers and append idempotent description sections
- GitHub (2): import_github_issues, link_task_to_github_issue
- Reindex (1): reindex_tasks
- Build (1): build_task
"""

from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.build import create_build_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_github import create_github_registry
from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry
from gobby.mcp_proxy.tools.tasks._artifacts import create_ops_artifact_registry
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


class _TaskOpsToolRegistry(InternalToolRegistry):
    """Ops registry with build_task schema exposed for the build MCP contract."""

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [dict(tool) for tool in super().list_tools()]
        schema = self.get_schema("build_task")
        if schema is None:
            return tools

        for tool in tools:
            if tool["name"] == "build_task":
                tool["inputSchema"] = schema["inputSchema"]
                break
        return tools


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

    registry = _TaskOpsToolRegistry(
        name="gobby-tasks-ops",
        description="Task operations - expansion, front-half orchestration, affected files, GitHub, reindex",
    )

    # Merge expansion tools
    registry.merge_from(create_expansion_registry(ctx))

    # Merge front-half conductor tools
    registry.merge_from(create_front_half_registry(ctx))

    # Merge ops affected files tools (set, get, find_overlaps, wire_from_run)
    registry.merge_from(create_ops_affected_files_registry(ctx))

    # Merge artifact mutation tools for merge/expansion-qa/holistic-reviewer agents
    registry.merge_from(create_ops_artifact_registry(ctx))

    # Merge GitHub integration tools (2 tools)
    registry.merge_from(create_github_registry(ctx))

    # Merge reindex tool (1 tool)
    registry.merge_from(create_reindex_registry(ctx))

    # Merge build automation entry point
    registry.merge_from(create_build_registry(ctx))

    return registry
