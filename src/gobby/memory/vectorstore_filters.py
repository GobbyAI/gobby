"""Qdrant filter construction for memory vector storage."""

from __future__ import annotations

from collections.abc import Mapping

from qdrant_client.models import FieldCondition, Filter, MatchValue

from gobby.storage.memories_models import MemoryType, validate_memory_type
from gobby.storage.memories_scope import MemoryScope, MemoryScopeKind


def payload_filter(filters: Mapping[str, str] | Filter | None) -> Filter | None:
    """Convert simple payload equality constraints to a Qdrant filter."""
    if filters is None or isinstance(filters, Filter):
        return filters
    conditions = [
        FieldCondition(key=key, match=MatchValue(value=value)) for key, value in filters.items()
    ]
    return Filter(must=conditions)


def memory_scope_filter(
    scope: MemoryScope, memory_type: str | MemoryType | None = None
) -> Filter | None:
    """Return a Qdrant filter for an explicit memory scope."""
    type_conditions = []
    if memory_type is not None:
        type_conditions.append(
            FieldCondition(
                key="memory_type",
                match=MatchValue(value=validate_memory_type(memory_type).value),
            )
        )
    if scope.kind is MemoryScopeKind.ALL:
        return Filter(must=type_conditions) if type_conditions else None
    global_condition = FieldCondition(key="is_global", match=MatchValue(value=True))
    if scope.kind is MemoryScopeKind.GLOBAL_ONLY:
        return Filter(must=[*type_conditions, global_condition])
    project_condition = FieldCondition(
        key="project_id",
        match=MatchValue(value=scope.project_id),
    )
    if scope.kind is MemoryScopeKind.OWNER:
        return Filter(must=[*type_conditions, project_condition])
    if scope.kind is MemoryScopeKind.PROJECT_ONLY:
        return Filter(
            must=[
                *type_conditions,
                project_condition,
                FieldCondition(key="is_global", match=MatchValue(value=False)),
            ]
        )
    return Filter(must=type_conditions or None, should=[project_condition, global_condition])
