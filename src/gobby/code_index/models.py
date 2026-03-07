"""Data models for code indexing."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Stable namespace for deterministic symbol UUIDs
CODE_INDEX_UUID_NAMESPACE = uuid.UUID("c0de1de0-0000-4000-8000-000000000000")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
            summary=row["summary"],
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
