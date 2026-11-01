"""Task tools package.

This package provides MCP tools for task management.

Public API:
    - create_task_registry: Factory function to create the task tool registry
    - resolve_task_id_for_mcp: Resolve task references to UUIDs

"""

from gobby.mcp_proxy.tools.tasks._factory import create_task_registry
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp

__all__ = [
    "create_task_registry",
    "resolve_task_id_for_mcp",
]
