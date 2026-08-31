"""Data models for code indexing."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from gobby.utils.datetime import normalize_datetime_model, utc_now

# Stable namespace for deterministic symbol UUIDs
CODE_INDEX_UUID_NAMESPACE = uuid.UUID("c0de1de0-0000-4000-8000-000000000000")


class IndexWriteMode(StrEnum):
    """Checkout validation policy for machine-local index selector writes."""

    PRIMARY = "primary"
    OVERLAY = "overlay"


def make_unresolved_callee_id(project_id: str, callee_name: str) -> str:
    """Generate a stable ID for an unresolved same-project callee."""
    key = f"unresolved:{project_id}:{callee_name}"
    return str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, key))


def make_external_symbol_id(project_id: str, callee_name: str, module: str | None = None) -> str:
    """Generate a stable ID for an external call target."""
    module_key = module or ""
    key = f"external:{project_id}:{module_key}:{callee_name}"
    return str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, key))


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=("summary_attempted_at",),
)
@dataclass
class Symbol:
    """A code symbol extracted from AST parsing.

    ``qualified_name`` is a container-qualified symbol path within one file, not
    a module- or package-qualified import path. Top-level symbols use ``name``
    unchanged; nested class/type members use ``Parent.member``. The Rust
    ``gcode`` writer is the production writer, and ``CodeIndexStorage`` tests
    use these model helpers as the Python reference contract.
    """

    id: str
    project_id: str
    file_path: str
    name: str
    qualified_name: str
    kind: str  # function, class, method, constant, type, import
    language: str
    byte_start: int
    byte_end: int
    line_start: int
    line_end: int
    signature: str | None = None
    docstring: str | None = None
    parent_symbol_id: str | None = None
    file_content_hash: str = ""
    content_hash: str = ""
    summary: str | None = None
    summary_attempted_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utc_now()
        if not self.updated_at:
            self.updated_at = utc_now()

    @staticmethod
    def make_id(
        project_id: str,
        file_path: str,
        file_content_hash: str,
        name: str,
        kind: str,
        byte_start: int,
    ) -> str:
        """Generate the UUID5 ID shared by Python tests and the Rust ``gcode`` writer."""
        key = f"{project_id}:{file_path}:{file_content_hash}:{name}:{kind}:{byte_start}"
        return str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, key))

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Symbol:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            file_path=row["file_path"],
            name=row["name"],
            qualified_name=row["qualified_name"],
            kind=row["kind"],
            language=row["language"],
            byte_start=row["byte_start"],
            byte_end=row["byte_end"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            signature=row["signature"],
            docstring=row["docstring"],
            parent_symbol_id=row["parent_symbol_id"],
            file_content_hash=row["file_content_hash"],
            content_hash=row["content_hash"],
            summary=row["summary"] if "summary" in row.keys() else None,
            summary_attempted_at=(
                row["summary_attempted_at"] if "summary_attempted_at" in row.keys() else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "file_path": self.file_path,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "language": self.language,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "signature": self.signature,
            "docstring": self.docstring,
            "parent_symbol_id": self.parent_symbol_id,
            "file_content_hash": self.file_content_hash,
            "content_hash": self.content_hash,
            "summary": self.summary,
            "summary_attempted_at": self.summary_attempted_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_brief(self) -> dict[str, Any]:
        """Minimal representation for search results — just enough to decide what to retrieve."""
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "signature": self.signature,
        }
        if self.summary:
            result["summary"] = self.summary
        elif self.docstring:
            first_line = self.docstring.split("\n", 1)[0].strip()
            if first_line:
                result["docstring"] = first_line
        if self.parent_symbol_id:
            result["parent_id"] = self.parent_symbol_id
        return result


@normalize_datetime_model(
    required=("indexed_at",),
    optional=(
        "graph_sync_attempted_at",
        "vector_sync_attempted_at",
    ),
)
@dataclass
class IndexedFile:
    """A file that has been indexed.

    Re-indexing a file marks graph/vector projections stale; the Rust ``gcode``
    writer and ``CodeIndexStorage.upsert_file`` must keep that flag contract in
    sync.
    """

    id: str
    project_id: str
    file_path: str
    language: str
    content_hash: str
    symbol_count: int = 0
    byte_size: int = 0
    graph_synced: bool = False
    vectors_synced: bool = False
    graph_sync_attempted_at: datetime | None = None
    vector_sync_attempted_at: datetime | None = None
    indexed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.indexed_at:
            self.indexed_at = utc_now()

    @staticmethod
    def make_id(project_id: str, file_path: str, content_hash: str) -> str:
        key = f"{project_id}:{file_path}:{content_hash}"
        return str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, key))

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> IndexedFile:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            file_path=row["file_path"],
            language=row["language"],
            content_hash=row["content_hash"],
            symbol_count=row["symbol_count"],
            byte_size=row["byte_size"],
            graph_synced=bool(row["graph_synced"]) if "graph_synced" in row.keys() else False,
            vectors_synced=bool(row["vectors_synced"]) if "vectors_synced" in row.keys() else False,
            graph_sync_attempted_at=(
                row["graph_sync_attempted_at"] if "graph_sync_attempted_at" in row.keys() else None
            ),
            vector_sync_attempted_at=(
                row["vector_sync_attempted_at"]
                if "vector_sync_attempted_at" in row.keys()
                else None
            ),
            indexed_at=row["indexed_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "file_path": self.file_path,
            "language": self.language,
            "content_hash": self.content_hash,
            "symbol_count": self.symbol_count,
            "byte_size": self.byte_size,
            "graph_synced": self.graph_synced,
            "vectors_synced": self.vectors_synced,
            "graph_sync_attempted_at": self.graph_sync_attempted_at,
            "vector_sync_attempted_at": self.vector_sync_attempted_at,
            "indexed_at": self.indexed_at,
        }


@normalize_datetime_model(required=("last_indexed_at",))
@dataclass
class IndexedProject:
    """Statistics for an indexed project."""

    id: str
    root_path: str
    total_files: int = 0
    total_symbols: int = 0
    last_indexed_at: datetime = field(default_factory=utc_now)
    index_duration_ms: int = 0

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> IndexedProject:
        return cls(
            id=row["id"],
            root_path=row["root_path"],
            total_files=row["total_files"],
            total_symbols=row["total_symbols"],
            last_indexed_at=row["last_indexed_at"] or utc_now(),
            index_duration_ms=row["index_duration_ms"] or 0,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "root_path": self.root_path,
            "total_files": self.total_files,
            "total_symbols": self.total_symbols,
            "last_indexed_at": self.last_indexed_at,
            "index_duration_ms": self.index_duration_ms,
        }


ProjectionCleanupStore = Literal["graph", "vector"]


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    )
)
@dataclass
class ProjectionCleanupPending:
    """Project-level projection cleanup that must be retried."""

    project_id: str
    store: ProjectionCleanupStore
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ProjectionCleanupPending:
        return cls(
            project_id=row["project_id"],
            store=row["store"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    )
)
@dataclass
class CodeIndexPruneDirtyProject:
    """Project root that needs a gcode prune retry."""

    project_id: str
    machine_id: str
    root_path: str
    reason: str
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> CodeIndexPruneDirtyProject:
        return cls(
            project_id=row["project_id"],
            machine_id=row["machine_id"],
            root_path=row["root_path"],
            reason=row["reason"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@normalize_datetime_model(required=("created_at",))
@dataclass
class ContentChunk:
    """A chunk of file content for full-text search."""

    id: str
    project_id: str
    file_path: str
    content_hash: str
    chunk_index: int
    line_start: int
    line_end: int
    content: str
    language: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utc_now()

    @staticmethod
    def make_id(project_id: str, file_path: str, content_hash: str, chunk_index: int) -> str:
        key = f"{project_id}:{file_path}:{content_hash}:chunk:{chunk_index}"
        return str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, key))


@dataclass
class ImportRelation:
    """An import statement linking files."""

    source_file: str
    target_module: str
    imported_names: list[str] = field(default_factory=list)


@dataclass
class CallRelation:
    """A function/method call linking symbols."""

    caller_symbol_id: str
    callee_name: str
    file_path: str
    line: int
    callee_symbol_id: str | None = None
    callee_target_kind: Literal["symbol", "unresolved", "external"] = "unresolved"
    callee_external_module: str | None = None

    def __post_init__(self) -> None:
        if self.callee_symbol_id:
            self.callee_target_kind = "symbol"


@dataclass
class ParseResult:
    """Result of parsing a single file."""

    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportRelation] = field(default_factory=list)
    calls: list[CallRelation] = field(default_factory=list)


@dataclass
class IndexResult:
    """Result of an indexing operation."""

    project_id: str
    files_indexed: int = 0
    files_skipped: int = 0
    symbols_found: int = 0
    symbols_embedded: int = 0
    relationships_added: int = 0
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "symbols_found": self.symbols_found,
            "symbols_embedded": self.symbols_embedded,
            "relationships_added": self.relationships_added,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
        }
