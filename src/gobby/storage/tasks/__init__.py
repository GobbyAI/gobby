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
    MissingIsolationBaseError,
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
from gobby.storage.tasks._build_cascade import cascade_build_state_to_subtree
from gobby.storage.tasks._dispatch_mutex import (
    DispatchMutex,
    TaskDispatchMutexManager,
    acquire_mutex,
    attach_run_id,
    clear_by_run_id,
    force_release,
    get_mutex,
    release_mutex,
    sweep_expired,
)
from gobby.storage.tasks._id import generate_task_id
from gobby.storage.tasks._lifecycle_events import (
    TaskLifecycleEvent,
    TaskLifecycleEventManager,
    list_lifecycle_events,
    record_lifecycle_event,
)
from gobby.storage.tasks._manager import LocalTaskManager
from gobby.storage.tasks._models import (
    PRIORITY_MAP,
    TASK_TYPE_CHOICES,
    UNSET,
    VALID_CATEGORIES,
    VALID_TASK_TYPES,
    Isolation,
    MaybeUnset,
    SeqNumCollisionError,
    Task,
    TaskAlreadyClaimedError,
    TaskAlreadyEscalatedError,
    TaskClosedError,
    TaskIDCollisionError,
    TaskNotFoundError,
    UnsetType,
    normalize_priority,
    validate_category,
    validate_implementation_domain,
    validate_task_type,
)
from gobby.storage.tasks._ordering import order_tasks_hierarchically
from gobby.storage.tasks._stage_registry import (
    ReviewPolicy,
    StageRegistryEntry,
    StageRegistryManager,
)
from gobby.storage.tasks._stage_states import (
    StageStatesManager,
)
from gobby.storage.tasks._stage_types import (
    IllegalManifestMutationError,
    IllegalStageTransitionError,
    ManifestAlreadyInitializedError,
    StageManifestSpec,
    StageState,
)
from gobby.tasks.categories import TDD_ELIGIBLE_CATEGORIES

__all__ = [
    # Core classes
    "Task",
    "LocalTaskManager",
    "Isolation",
    "DispatchMutex",
    "TaskDispatchMutexManager",
    "TaskArtifacts",
    "TaskArtifactManager",
    "TaskLifecycleEvent",
    "TaskLifecycleEventManager",
    "StageRegistryEntry",
    "StageRegistryManager",
    "StageManifestSpec",
    "StageState",
    "StageStatesManager",
    # Exceptions
    "SeqNumCollisionError",
    "TaskIDCollisionError",
    "TaskNotFoundError",
    "TaskClosedError",
    "TaskAlreadyEscalatedError",
    "TaskAlreadyClaimedError",
    "TaskArtifactConstraintError",
    "ArtifactCheckConstraintError",
    "MissingIsolationBaseError",
    "IllegalManifestMutationError",
    "IllegalStageTransitionError",
    "ManifestAlreadyInitializedError",
    # Functions
    "generate_task_id",
    "validate_category",
    "validate_implementation_domain",
    "normalize_priority",
    "order_tasks_hierarchically",
    "cascade_build_state_to_subtree",
    "get_mutex",
    "acquire_mutex",
    "attach_run_id",
    "force_release",
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
    "record_lifecycle_event",
    "list_lifecycle_events",
    # Constants
    "PRIORITY_MAP",
    "TASK_TYPE_CHOICES",
    "TDD_ELIGIBLE_CATEGORIES",
    "VALID_CATEGORIES",
    "VALID_TASK_TYPES",
    "MaybeUnset",
    "UNSET",
    "UnsetType",
    "ReviewPolicy",
    "validate_task_type",
]
