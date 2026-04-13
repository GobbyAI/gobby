"""
Task management components.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.tasks.validation import TaskValidator, ValidationResult


def __getattr__(name: str) -> Any:
    if name in {"TaskValidator", "ValidationResult"}:
        from gobby.tasks.validation import TaskValidator, ValidationResult

        exports = {
            "TaskValidator": TaskValidator,
            "ValidationResult": ValidationResult,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["TaskValidator", "ValidationResult"]
