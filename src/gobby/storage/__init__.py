"""Local storage layer for Gobby daemon."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.communications import LocalCommunicationsStore as LocalCommunicationsStore
    from gobby.storage.database import LocalDatabase as LocalDatabase
    from gobby.storage.delivery import TaskDeliveryStateManager as TaskDeliveryStateManager
    from gobby.storage.expansion_runs import LocalExpansionRunManager as LocalExpansionRunManager
    from gobby.storage.inter_session_messages import (
        InterSessionMessageManager as InterSessionMessageManager,
    )
    from gobby.storage.mcp import LocalMCPManager as LocalMCPManager
    from gobby.storage.plans import LocalPlanManager as LocalPlanManager
    from gobby.storage.projects import LocalProjectManager as LocalProjectManager
    from gobby.storage.sessions import SessionManager as SessionManager
    from gobby.storage.task_dependencies import TaskDependencyManager as TaskDependencyManager
    from gobby.storage.tasks import LocalTaskManager as LocalTaskManager

__all__ = [
    "InterSessionMessageManager",
    "LocalCommunicationsStore",
    "LocalDatabase",
    "LocalExpansionRunManager",
    "LocalMCPManager",
    "LocalPlanManager",
    "LocalProjectManager",
    "SessionManager",
    "LocalTaskManager",
    "TaskDependencyManager",
    "TaskDeliveryStateManager",
]

_EXPORTS = {
    "InterSessionMessageManager": (
        "gobby.storage.inter_session_messages",
        "InterSessionMessageManager",
    ),
    "LocalCommunicationsStore": ("gobby.storage.communications", "LocalCommunicationsStore"),
    "LocalDatabase": ("gobby.storage.database", "LocalDatabase"),
    "TaskDeliveryStateManager": ("gobby.storage.delivery", "TaskDeliveryStateManager"),
    "LocalExpansionRunManager": ("gobby.storage.expansion_runs", "LocalExpansionRunManager"),
    "LocalMCPManager": ("gobby.storage.mcp", "LocalMCPManager"),
    "LocalPlanManager": ("gobby.storage.plans", "LocalPlanManager"),
    "LocalProjectManager": ("gobby.storage.projects", "LocalProjectManager"),
    "SessionManager": ("gobby.storage.sessions", "SessionManager"),
    "LocalTaskManager": ("gobby.storage.tasks", "LocalTaskManager"),
    "TaskDependencyManager": ("gobby.storage.task_dependencies", "TaskDependencyManager"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
