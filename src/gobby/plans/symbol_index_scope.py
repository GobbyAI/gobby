"""Checkout scope and immutable index selection for plan validation."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from gobby.code_index.models import CODE_INDEX_UUID_NAMESPACE


class SymbolIndexStorage(Protocol):
    """Storage surface needed to validate indexed plan targets."""

    def get_project_stats(self, project_id: str) -> Any | None: ...

    def get_file(self, project_id: str, file_path: str) -> Any | None: ...

    def get_pinned_file(self, project_id: str, file_path: str) -> tuple[str, Any] | None: ...

    def get_symbols_for_file(
        self, project_id: str, file_path: str, *, content_hash: str | None = None
    ) -> list[Any]: ...

    def get_symbol_usages(self, project_id: str, symbol_id: str) -> list[str]: ...


@dataclass(frozen=True)
class SymbolValidationScope:
    """Resolved filesystem and code-index scope for symbol validation."""

    filesystem_root: Path
    primary_project_id: str
    parent_project_id: str | None = None

    @property
    def project_ids(self) -> tuple[str, ...]:
        if self.parent_project_id is None:
            return (self.primary_project_id,)
        return (self.primary_project_id, self.parent_project_id)


def index_storage(code_index: Any) -> SymbolIndexStorage:
    storage = getattr(code_index, "storage", code_index)
    return cast(SymbolIndexStorage, storage)


def visible_indexed_file(
    storage: SymbolIndexStorage, scope: SymbolValidationScope, file_path: str
) -> tuple[str, Any | None]:
    indexed_file = storage.get_file(scope.primary_project_id, file_path)
    if indexed_file is None and scope.parent_project_id is not None:
        pinned = storage.get_pinned_file(scope.primary_project_id, file_path)
        if pinned is not None:
            return pinned
    return scope.primary_project_id, indexed_file


def resolve_validation_scope(
    project_context: Mapping[str, Any] | None,
    *,
    expected_project_id: str | None,
) -> tuple[SymbolValidationScope | None, str | None]:
    if project_context is None:
        return None, "project context is missing"

    project_id = _nonempty_context_string(project_context.get("id"))
    project_path = _nonempty_context_string(project_context.get("project_path"))
    if project_id is None or project_path is None:
        return None, "project context requires id and project_path"

    parent_id_value = project_context.get("parent_project_id")
    parent_path_value = project_context.get("parent_project_path")
    has_parent_id = parent_id_value is not None
    has_parent_path = parent_path_value is not None
    if has_parent_id != has_parent_path:
        return None, "isolation context requires parent_project_id and parent_project_path together"

    filesystem_root = _canonical_root(Path(project_path))
    logical_project_id = project_id
    primary_project_id = project_id
    parent_project_id: str | None = None
    if has_parent_id:
        parent_project_id = _nonempty_context_string(parent_id_value)
        parent_project_path = _nonempty_context_string(parent_path_value)
        if parent_project_id is None or parent_project_path is None:
            return None, "isolation parent fields must be non-empty strings"
        if project_id != parent_project_id:
            return None, "isolation context id does not match parent_project_id"
        logical_project_id = parent_project_id
        primary_project_id = str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, str(filesystem_root)))

    if expected_project_id is not None and expected_project_id != logical_project_id:
        return (
            None,
            f"project context does not match the requested logical project {expected_project_id}",
        )
    return (
        SymbolValidationScope(
            filesystem_root=filesystem_root,
            primary_project_id=primary_project_id,
            parent_project_id=parent_project_id,
        ),
        None,
    )


def _nonempty_context_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _canonical_root(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()
