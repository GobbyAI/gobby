"""Factory function for creating the task tool registry.

Orchestrates the creation of all task tool sub-registries and merges them
into a unified registry.
"""

from typing import TYPE_CHECKING

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry
from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry
from gobby.mcp_proxy.tools.task_sync import create_sync_registry
from gobby.mcp_proxy.tools.task_validation import create_validation_registry
from gobby.mcp_proxy.tools.tasks._affected_files import create_affected_files_registry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._crud import create_crud_registry
from gobby.mcp_proxy.tools.tasks._expansion import create_expansion_registry
from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry
from gobby.mcp_proxy.tools.tasks._search import create_search_registry
from gobby.mcp_proxy.tools.tasks._session import create_session_registry
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager
from gobby.tasks.validation import TaskValidator

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.config.app import DaemonConfig


def create_task_registry(
    task_manager: LocalTaskManager,
    sync_manager: TaskSyncManager,
    task_validator: TaskValidator | None = None,
    config: "DaemonConfig | None" = None,
    agent_runner: "AgentRunner | None" = None,
    project_id: str | None = None,
) -> InternalToolRegistry:
    """
    Create a task tool registry with all task-related tools.

    Args:
        task_manager: LocalTaskManager instance
        sync_manager: TaskSyncManager instance
        task_validator: TaskValidator instance (optional)
        config: DaemonConfig instance (optional)
        agent_runner: AgentRunner instance for external validator agent mode (optional)
        project_id: Default project ID (optional)

    Returns:
        InternalToolRegistry with all task tools registered
    """
    # Create the shared context
    ctx = RegistryContext(
        task_manager=task_manager,
        sync_manager=sync_manager,
        task_validator=task_validator,
        agent_runner=agent_runner,
        config=config,
    )

    # Create the main registry
    registry = InternalToolRegistry(
        name="gobby-tasks",
        description="Task management - CRUD, dependencies, sync",
    )

    # Merge CRUD tools
    crud_registry = create_crud_registry(ctx)
    for tool_name, tool in crud_registry._tools.items():
        registry._tools[tool_name] = tool

    # Merge lifecycle tools
    lifecycle_registry = create_lifecycle_registry(ctx)
    for tool_name, tool in lifecycle_registry._tools.items():
        registry._tools[tool_name] = tool

    # Merge session tools
    session_registry = create_session_registry(ctx)
    for tool_name, tool in session_registry._tools.items():
        registry._tools[tool_name] = tool

    # Merge search tools
    search_registry = create_search_registry(ctx)
    for tool_name, tool in search_registry._tools.items():
        registry._tools[tool_name] = tool

    # Merge expansion tools (skill-based task decomposition)
    expansion_registry = create_expansion_registry(ctx)
    for tool_name, tool in expansion_registry._tools.items():
        registry._tools[tool_name] = tool

    # Merge validation tools from extracted module (Strangler Fig pattern)
    validation_max_retries = 3
    if config:
        validation_max_retries = config.gobby_tasks.validation.max_retries
    validation_registry = create_validation_registry(
        task_manager=task_manager,
        task_validator=task_validator,
        project_manager=ctx.project_manager,
        get_project_repo_path=ctx.get_project_repo_path,
        max_retries=validation_max_retries,
    )
    for tool_name, tool in validation_registry._tools.items():
        registry._tools[tool_name] = tool

    # Merge dependency tools from extracted module (Strangler Fig pattern)
    dependency_registry = create_dependency_registry(
        task_manager=task_manager,
        dep_manager=ctx.dep_manager,
    )
    for tool_name, tool in dependency_registry._tools.items():
        registry._tools[tool_name] = tool

    # Merge readiness tools from extracted module (Strangler Fig pattern)
    readiness_registry = create_readiness_registry(
        task_manager=task_manager,
    )
    for tool_name, tool in readiness_registry._tools.items():
        registry._tools[tool_name] = tool

    # Merge affected files tools
    affected_files_registry = create_affected_files_registry(ctx)
    for tool_name, tool in affected_files_registry._tools.items():
        registry._tools[tool_name] = tool

    # Merge sync tools from extracted module (Strangler Fig pattern)
    from gobby.tasks.commits import auto_link_commits as auto_link_commits_fn
    from gobby.tasks.commits import get_task_diff

    sync_registry = create_sync_registry(
        sync_manager=sync_manager,
        task_manager=task_manager,
        project_manager=ctx.project_manager,
        auto_link_commits_fn=auto_link_commits_fn,
        get_task_diff_fn=get_task_diff,
        session_manager=ctx.session_manager,
    )
    for tool_name, tool in sync_registry._tools.items():
        registry._tools[tool_name] = tool

    return registry
