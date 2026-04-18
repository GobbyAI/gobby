"""Workflow package exports.

Keep package imports lazy so importing a specific workflow submodule does not
pull in the full workflow definition graph during package initialization.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.workflows.definitions import WorkflowDefinition as WorkflowDefinition

__all__ = ["WorkflowDefinition"]


def __getattr__(name: str) -> Any:
    if name == "WorkflowDefinition":
        from gobby.workflows.definitions import WorkflowDefinition

        globals()[name] = WorkflowDefinition
        return WorkflowDefinition
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
