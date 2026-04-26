"""Task storage module.

This package provides task management functionality including:
- Task dataclass and serialization
- LocalTaskManager for CRUD operations
- Task ID generation and resolution
- Hierarchical ordering utilities

All public symbols are re-exported for backward compatibility.
"""

from gobby.storage.tasks._artifacts import (
    ArtifactCheckConstraintError,
    TaskArtifactConstraintError,
    TaskArtifactManager,
    TaskArtifacts,
    clear_artifact,
    clear_artifacts,
    clear_isolation_pair,
    get_artifacts,
    increment_expansion_attempts,
    set_artifact,
    set_artifacts_atomic,
)
from gobby.storage.tasks._dispatch_mutex import (
    DispatchMutex,
    TaskDispatchMutexManager,
    acquire_mutex,
    clear_by_run_id,
    get_mutex,
    release_mutex,
    sweep_expired,
)
from gobby.storage.tasks._id import generate_task_id
from gobby.storage.tasks._manager import LocalTaskManager
from gobby.storage.tasks._models import (
    PRIORITY_MAP,
    UNSET,
    VALID_CATEGORIES,
    Isolation,
    Lifecycle,
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
    "Lifecycle",
    "Isolation",
    "DispatchMutex",
    "TaskDispatchMutexManager",
    "TaskArtifacts",
    "TaskArtifactManager",
    # Exceptions
    "SeqNumCollisionError",
    "TaskIDCollisionError",
    "TaskNotFoundError",
    "TaskArtifactConstraintError",
    "ArtifactCheckConstraintError",
    # Functions
    "generate_task_id",
    "validate_category",
    "normalize_priority",
    "order_tasks_hierarchically",
    "get_mutex",
    "acquire_mutex",
    "release_mutex",
    "clear_by_run_id",
    "sweep_expired",
    "get_artifacts",
    "set_artifact",
    "set_artifacts_atomic",
    "clear_artifact",
    "clear_artifacts",
    "clear_isolation_pair",
    "increment_expansion_attempts",
    # Constants
    "PRIORITY_MAP",
    "VALID_CATEGORIES",
    "MaybeUnset",
    "UNSET",
    "UnsetType",
]
