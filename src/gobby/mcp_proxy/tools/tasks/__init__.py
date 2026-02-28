"""Task tools package.

This package provides MCP tools for task management. Re-exports maintain
backwards compatibility with the original tasks.py module.

Public API:
    - create_task_registry: Factory function to create the task tool registry
    - resolve_task_id_for_mcp: Resolve task references to UUIDs

Internal (exported for test compatibility):
    - SKIP_REASONS: Reasons that skip validation on close
"""

from gobby.mcp_proxy.tools.tasks._factory import create_task_registry
from gobby.mcp_proxy.tools.tasks._helpers import SKIP_REASONS
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp

__all__ = [
    "create_task_registry",
    "resolve_task_id_for_mcp",
    # Internal exports for backward compatibility
    "SKIP_REASONS",
]
