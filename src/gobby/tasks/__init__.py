"""
Task management components.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.tasks.validation import TaskValidator
    from gobby.tasks.validation_verdict import ValidationResult


def __getattr__(name: str) -> Any:
    if name == "TaskValidator":
        from gobby.tasks.validation import TaskValidator

        return TaskValidator
    if name == "ValidationResult":
        from gobby.tasks.validation_verdict import ValidationResult

        return ValidationResult
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["TaskValidator", "ValidationResult"]
