"""Typed ownership and visibility scopes for memory storage."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MemoryScopeKind(StrEnum):
    """Supported memory query scopes."""

    ALL = "all"
    PROJECT_VISIBLE = "project_visible"
    PROJECT_ONLY = "project_only"
    GLOBAL_ONLY = "global_only"
    OWNER = "owner"


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Explicit memory query scope with no NULL sentinel semantics."""

    kind: MemoryScopeKind
    project_id: str | None = None

    def __post_init__(self) -> None:
        requires_project = self.kind in {
            MemoryScopeKind.PROJECT_VISIBLE,
            MemoryScopeKind.PROJECT_ONLY,
            MemoryScopeKind.OWNER,
        }
        if requires_project and not self.project_id:
            raise ValueError(f"project_id is required for {self.kind.value} memory scope")
        if not requires_project and self.project_id is not None:
            raise ValueError(f"project_id is invalid for {self.kind.value} memory scope")

    @classmethod
    def all(cls) -> "MemoryScope":
        return cls(MemoryScopeKind.ALL)

    @classmethod
    def project_visible(cls, project_id: str) -> "MemoryScope":
        return cls(MemoryScopeKind.PROJECT_VISIBLE, project_id)

    @classmethod
    def project_only(cls, project_id: str) -> "MemoryScope":
        return cls(MemoryScopeKind.PROJECT_ONLY, project_id)

    @classmethod
    def global_only(cls) -> "MemoryScope":
        return cls(MemoryScopeKind.GLOBAL_ONLY)

    @classmethod
    def owner(cls, project_id: str) -> "MemoryScope":
        return cls(MemoryScopeKind.OWNER, project_id)


ALL_MEMORIES = MemoryScope.all()
GLOBAL_MEMORIES = MemoryScope.global_only()


def memory_scope_predicate(
    scope: MemoryScope,
    *,
    table_alias: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """Return a SQL predicate and parameters for an explicit memory scope."""
    prefix = f"{table_alias}." if table_alias else ""
    if scope.kind is MemoryScopeKind.ALL:
        return "", ()
    if scope.kind is MemoryScopeKind.GLOBAL_ONLY:
        return f"{prefix}is_global IS TRUE", ()
    if scope.kind is MemoryScopeKind.OWNER:
        return f"{prefix}project_id = %s", (scope.project_id,)
    if scope.kind is MemoryScopeKind.PROJECT_ONLY:
        return (
            f"{prefix}project_id = %s AND {prefix}is_global IS FALSE",
            (scope.project_id,),
        )
    return (
        f"({prefix}project_id = %s OR {prefix}is_global IS TRUE)",
        (scope.project_id,),
    )


def memory_matches_scope(project_id: str, is_global: bool, scope: MemoryScope) -> bool:
    """Return whether a memory owner/visibility pair belongs to ``scope``."""
    if scope.kind is MemoryScopeKind.ALL:
        return True
    if scope.kind is MemoryScopeKind.GLOBAL_ONLY:
        return is_global
    if scope.kind is MemoryScopeKind.OWNER:
        return project_id == scope.project_id
    if scope.kind is MemoryScopeKind.PROJECT_ONLY:
        return project_id == scope.project_id and not is_global
    return project_id == scope.project_id or is_global
