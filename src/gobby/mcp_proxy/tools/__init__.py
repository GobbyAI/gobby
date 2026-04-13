"""
MCP proxy tools module.

Provides factory functions for creating tool registries.
"""

# Main task registry (facade that merges all task-related registries)
# Extracted task module registries (for direct use or testing)
from gobby.mcp_proxy.tools.task_dependencies import create_dependency_registry
from gobby.mcp_proxy.tools.task_readiness import create_readiness_registry
from gobby.mcp_proxy.tools.task_sync import create_commit_registry
from gobby.mcp_proxy.tools.task_validation import create_validation_registry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry

__all__ = [
    # Main facade
    "create_task_registry",
    "create_task_ops_registry",
    # Extracted registries
    "create_dependency_registry",
    "create_readiness_registry",
    "create_commit_registry",
    "create_validation_registry",
]
