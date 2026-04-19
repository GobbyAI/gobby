"""Data models for code indexing."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

# Stable namespace for deterministic symbol UUIDs
CODE_INDEX_UUID_NAMESPACE = uuid.UUID("c0de1de0-0000-4000-8000-000000000000")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def make_unresolved_callee_id(project_id: str, callee_name: str) -> str:
    """Generate a stable ID for an unresolved same-project callee."""
    key = f"unresolved:{project_id}:{callee_name}"
    return str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, key))


def make_external_symbol_id(project_id: str, callee_name: str, module: str | None = None) -> str:
    """Generate a stable ID for an external call target."""
    module_key = module or ""
    key = f"external:{project_id}:{module_key}:{callee_name}"
    return str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, key))


@dataclass
class Symbol:
    """A code symbol extracted from AST parsing."""

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
    content_hash: str = ""
    summary: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = _now_iso()

    @staticmethod
    def make_id(project_id: str, file_path: str, name: str, kind: str, byte_start: int) -> str:
        """Generate deterministic UUID5 for a symbol."""
        key = f"{project_id}:{file_path}:{name}:{kind}:{byte_start}"
        return str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, key))

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Symbol:
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
            content_hash=row["content_hash"],
            summary=row["summary"] if "summary" in row.keys() else None,
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
            "content_hash": self.content_hash,
            "summary": self.summary,
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


@dataclass
class IndexedFile:
    """A file that has been indexed."""

    id: str
    project_id: str
    file_path: str
    language: str
    content_hash: str
    symbol_count: int = 0
    byte_size: int = 0
    graph_synced: int = 0
    vectors_synced: int = 0
    graph_sync_attempted_at: str | None = None
    indexed_at: str = ""

    def __post_init__(self) -> None:
        if not self.indexed_at:
            self.indexed_at = _now_iso()

    @staticmethod
    def make_id(project_id: str, file_path: str) -> str:
        key = f"{project_id}:{file_path}"
        return str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, key))

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> IndexedFile:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            file_path=row["file_path"],
            language=row["language"],
            content_hash=row["content_hash"],
            symbol_count=row["symbol_count"],
            byte_size=row["byte_size"],
            graph_synced=row["graph_synced"] if "graph_synced" in row.keys() else 0,
            vectors_synced=row["vectors_synced"] if "vectors_synced" in row.keys() else 0,
            graph_sync_attempted_at=(
                row["graph_sync_attempted_at"] if "graph_sync_attempted_at" in row.keys() else None
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
            "indexed_at": self.indexed_at,
        }


@dataclass
class IndexedProject:
    """Statistics for an indexed project."""

    id: str
    root_path: str
    total_files: int = 0
    total_symbols: int = 0
    last_indexed_at: str = ""
    index_duration_ms: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> IndexedProject:
        return cls(
            id=row["id"],
            root_path=row["root_path"],
            total_files=row["total_files"],
            total_symbols=row["total_symbols"],
            last_indexed_at=row["last_indexed_at"] or "",
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


@dataclass
class ContentChunk:
    """A chunk of file content for full-text search."""

    id: str
    project_id: str
    file_path: str
    chunk_index: int
    line_start: int
    line_end: int
    content: str
    language: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now_iso()

    @staticmethod
    def make_id(project_id: str, file_path: str, chunk_index: int) -> str:
        key = f"{project_id}:{file_path}:chunk:{chunk_index}"
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
