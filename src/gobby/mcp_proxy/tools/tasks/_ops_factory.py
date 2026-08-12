"""Factory function for creating the task ops tool registry.

Orchestrates the creation of cold-path task tool sub-registries and merges
them into a unified gobby-tasks-ops registry. This server contains tools
used by pipelines and occasional manual operations, not daily-driver
session work.

Tools included:
- Expansion: start/get/latest/resume/cancel/validate run, QA result, QA coverage, plan validation
- Affected files (4): set, get, find_overlaps, wire_from_run
- Artifacts (5): set/get artifact pointers and append idempotent description sections
- GitHub (2): import_github_issues, link_task_to_github_issue
- Reindex (1): reindex_tasks
- Build (1): build_task
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.build import create_build_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_github import create_github_registry
from gobby.mcp_proxy.tools.tasks._affected_files import create_ops_affected_files_registry
from gobby.mcp_proxy.tools.tasks._artifacts import create_ops_artifact_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._delivery import create_delivery_registry
from gobby.mcp_proxy.tools.tasks._expansion import create_expansion_registry
from gobby.mcp_proxy.tools.tasks._search import create_reindex_registry
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.mcp_proxy.tools.tasks._stage_registry_ops import create_stage_registry_ops_registry
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.validation import TaskValidator

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.llm.service import LLMService
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.review_learning.service import ReviewLearningService


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
    task_validator_resolver: "Callable[[], TaskValidator | None] | None" = None,
    startup_config: "DaemonConfig | None" = None,
    config_resolver: "Callable[[], DaemonConfig | None] | None" = None,
    llm_service_resolver: "Callable[[], LLMService | None] | None" = None,
    completion_registry: "CompletionEventRegistry | None" = None,
    mcp_manager_resolver: "Callable[[], MCPClientManager | None] | None" = None,
    review_learning_service: "ReviewLearningService | None" = None,
) -> InternalToolRegistry:
    """Create a task ops tool registry with cold-path task tools.

    Args:
        task_manager: LocalTaskManager instance
        task_validator_resolver: per-call resolver for the current TaskValidator (optional)
        startup_config: DaemonConfig fallback before runtime readiness
        config_resolver: per-operation current DaemonConfig resolver

    Returns:
        InternalToolRegistry with ops task tools registered
    """
    # Create own RegistryContext (lightweight, shares the same manager objects)
    ctx = RegistryContext(
        task_manager=task_manager,
        task_validator_resolver=task_validator_resolver,
        startup_config=startup_config,
        config_resolver=config_resolver,
        llm_service_resolver=llm_service_resolver,
        completion_registry=completion_registry,
        mcp_manager_resolver=mcp_manager_resolver,
        review_learning_service=review_learning_service,
    )

    registry = _TaskOpsToolRegistry(
        name="gobby-tasks-ops",
        description="Task operations - expansion, affected files, GitHub, reindex",
    )

    # Merge expansion tools
    registry.merge_from(create_expansion_registry(ctx))

    # Merge ops affected files tools (set, get, find_overlaps, wire_from_run)
    registry.merge_from(create_ops_affected_files_registry(ctx))

    # Merge artifact mutation tools for merge/expansion-qa/epic-reviewer agents
    registry.merge_from(create_ops_artifact_registry(ctx))

    # Merge mutating stage manifest tools (read-only stage tools live in gobby-tasks)
    registry.merge_from(create_stage_ops_registry(ctx))

    # Merge stage-registry configuration tools
    registry.merge_from(create_stage_registry_ops_registry(ctx))

    # Merge PR/merge delivery-state tools
    registry.merge_from(create_delivery_registry(ctx))

    # Merge GitHub integration tools (2 tools)
    registry.merge_from(create_github_registry(ctx))

    # Merge reindex tool (1 tool)
    registry.merge_from(create_reindex_registry(ctx))

    # Merge build automation entry point
    registry.merge_from(create_build_registry(ctx))

    return registry
