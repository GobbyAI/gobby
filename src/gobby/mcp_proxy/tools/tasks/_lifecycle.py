"""Lifecycle operations for task management.

Thin orchestration shim that delegates to focused submodules:
- _lifecycle_close: close_task
- _lifecycle_claim: claim_task
- _lifecycle_status: reopen, escalate, de_escalate
- _lifecycle_delete: delete_task
- _lifecycle_labels: add_label, remove_label
"""

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._helpers import _is_uuid  # noqa: F401 (re-export)
from gobby.mcp_proxy.tools.tasks._lifecycle_claim import register_claim_task
from gobby.mcp_proxy.tools.tasks._lifecycle_close import register_close_task
from gobby.mcp_proxy.tools.tasks._lifecycle_delete import register_delete_task
from gobby.mcp_proxy.tools.tasks._lifecycle_labels import (
    register_add_label,
    register_remove_label,
)
from gobby.mcp_proxy.tools.tasks._lifecycle_status import (
    register_de_escalate_task,
    register_escalate_task,
    register_reopen_task,
)


def create_lifecycle_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with task lifecycle tools.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with lifecycle tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-tasks-lifecycle",
        description="Task lifecycle operations",
    )

    register_close_task(registry, ctx)
    register_reopen_task(registry, ctx)
    register_delete_task(registry, ctx)
    register_add_label(registry, ctx)
    register_remove_label(registry, ctx)
    register_claim_task(registry, ctx)
    register_escalate_task(registry, ctx)
    register_de_escalate_task(registry, ctx)

    return registry
