"""Factory function for creating the task tool registry.

Orchestrates the creation of all task tool sub-registries and merges them
into a unified registry.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_commits import create_commit_registry
from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry
from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry
from gobby.mcp_proxy.tools.tasks._affected_files import create_core_affected_files_registry
from gobby.mcp_proxy.tools.tasks._backup import create_backup_registry
from gobby.mcp_proxy.tools.tasks._build_observability import (
    create_build_observability_registry,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._crud import create_crud_registry
from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry
from gobby.mcp_proxy.tools.tasks._search import create_search_registry
from gobby.mcp_proxy.tools.tasks._session import create_session_registry
from gobby.mcp_proxy.tools.tasks._stage_read import create_stage_read_registry
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.validation import TaskValidator

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.review_learning.service import ReviewLearningService


def create_task_registry(
    task_manager: LocalTaskManager,
    task_validator_resolver: Callable[[], TaskValidator | None] | None = None,
    startup_config: "DaemonConfig | None" = None,
    config_resolver: "Callable[[], DaemonConfig | None] | None" = None,
    project_id: str | None = None,
    review_learning_service: "ReviewLearningService | None" = None,
    completion_registry: "CompletionEventRegistry | None" = None,
    agent_registry_resolver: "Callable[[], InternalToolRegistry | None] | None" = None,
) -> InternalToolRegistry:
    """
    Create a task tool registry with all task-related tools.

    Args:
        task_manager: LocalTaskManager instance
        task_validator_resolver: per-call resolver for the current TaskValidator (optional)
        startup_config: DaemonConfig fallback before runtime readiness
        config_resolver: per-operation current DaemonConfig resolver
        project_id: Default project ID (optional)

    Returns:
        InternalToolRegistry with all task tools registered
    """
    # Create the shared context
    ctx = RegistryContext(
        task_manager=task_manager,
        task_validator_resolver=task_validator_resolver,
        startup_config=startup_config,
        config_resolver=config_resolver,
        review_learning_service=review_learning_service,
        completion_registry=completion_registry,
        agent_registry_resolver=agent_registry_resolver,
    )

    # Create the main registry
    registry = InternalToolRegistry(
        name="gobby-tasks",
        description="Task management - CRUD, dependencies, backup and restore",
    )

    # Merge CRUD tools
    registry.merge_from(create_crud_registry(ctx))

    # Merge lifecycle tools (review stage transitions live in gobby-tasks-ops).
    registry.merge_from(create_lifecycle_registry(ctx))

    # Merge session tools
    registry.merge_from(create_session_registry(ctx))

    # Merge search tools (search_tasks only; reindex_tasks is in gobby-tasks-ops)
    registry.merge_from(create_search_registry(ctx))

    # Merge dependency tools from extracted module (Strangler Fig pattern)
    registry.merge_from(
        create_dependency_registry(
            task_manager=task_manager,
            dep_manager=ctx.dep_manager,
        )
    )

    # Merge readiness tools from extracted module (Strangler Fig pattern)
    registry.merge_from(create_readiness_registry(task_manager=task_manager))

    # Merge core affected files tools (update_observed_files only; rest in gobby-tasks-ops)
    registry.merge_from(create_core_affected_files_registry(ctx))

    # Merge read-only stage manifest tools (mutating stage tools live in gobby-tasks-ops)
    registry.merge_from(create_stage_read_registry(ctx))

    # Merge read-only build observability tools (build_task lives in gobby-tasks-ops)
    registry.merge_from(create_build_observability_registry(ctx))

    # Merge explicit JSONL backup and restore tools.
    registry.merge_from(create_backup_registry(ctx))

    # Merge commit linking tools.
    from gobby.tasks.commits import auto_link_commits as auto_link_commits_fn
    from gobby.tasks.diff_paging import get_task_diff_page

    registry.merge_from(
        create_commit_registry(
            task_manager=task_manager,
            project_manager=ctx.project_manager,
            auto_link_commits_fn=auto_link_commits_fn,
            get_task_diff_page_fn=get_task_diff_page,
            session_manager=ctx.session_manager,
        )
    )

    return registry
