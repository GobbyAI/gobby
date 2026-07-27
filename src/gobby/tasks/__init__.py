"""
Task management components.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.tasks.validation import TaskValidator


def __getattr__(name: str) -> Any:
    if name == "TaskValidator":
        from gobby.tasks.validation import TaskValidator

        return TaskValidator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["TaskValidator"]
