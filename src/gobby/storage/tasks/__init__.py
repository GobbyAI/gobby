"""Task storage module.

This package provides task management functionality including:
- Task dataclass and serialization
- LocalTaskManager for CRUD operations
- Task ID generation and resolution
- Hierarchical ordering utilities

All public symbols are re-exported for backward compatibility.
"""

from gobby.storage.tasks._id import generate_task_id
from gobby.storage.tasks._manager import LocalTaskManager
from gobby.storage.tasks._models import (
    PRIORITY_MAP,
    UNSET,
    VALID_CATEGORIES,
    MaybeUnset,
    SeqNumCollisionError,
    Task,
    TaskIDCollisionError,
    TaskNotFoundError,
    UnsetType,
    normalize_priority,
    validate_category,
)
from gobby.storage.tasks._ordering import order_tasks_hierarchically

__all__ = [
    # Core classes
    "Task",
    "LocalTaskManager",
    # Exceptions
    "SeqNumCollisionError",
    "TaskIDCollisionError",
    "TaskNotFoundError",
    # Functions
    "generate_task_id",
    "validate_category",
    "normalize_priority",
    "order_tasks_hierarchically",
    # Constants
    "PRIORITY_MAP",
    "VALID_CATEGORIES",
    "MaybeUnset",
    "UNSET",
    "UnsetType",
]
