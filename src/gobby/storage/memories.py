from gobby.storage.memories_crossrefs import MemoryCrossRefMixin
from gobby.storage.memories_crud import MemoryCrudMixin
from gobby.storage.memories_dreams import MemoryDreamMixin
from gobby.storage.memories_graph import MemoryGraphMixin
from gobby.storage.memories_models import (
    MEMORY_TYPE_VALUES,
    Memory,
    MemoryCrossRef,
    MemoryType,
    Visibility,
    validate_memory_type,
    visibility_predicate,
)
from gobby.storage.memories_models import MEMORY_UUID_NAMESPACE as MEMORY_UUID_NAMESPACE
from gobby.storage.memories_query import MemoryQueryMixin
from gobby.storage.memories_scope import (
    ALL_MEMORIES,
    GLOBAL_MEMORIES,
    MemoryScope,
    MemoryScopeKind,
    memory_matches_scope,
    memory_scope_predicate,
)

__all__ = [
    "Memory",
    "MemoryCrossRef",
    "MemoryType",
    "MEMORY_TYPE_VALUES",
    "LocalMemoryManager",
    "ALL_MEMORIES",
    "GLOBAL_MEMORIES",
    "MemoryScope",
    "MemoryScopeKind",
    "Visibility",
    "memory_matches_scope",
    "memory_scope_predicate",
    "validate_memory_type",
    "visibility_predicate",
]


class LocalMemoryManager(
    MemoryCrudMixin,
    MemoryDreamMixin,
    MemoryGraphMixin,
    MemoryQueryMixin,
    MemoryCrossRefMixin,
):
    """PostgreSQL-backed local memory storage facade."""
