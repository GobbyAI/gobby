"""Native AST-based code indexing for Gobby.

Provides tree-sitter parsing, symbol extraction, hybrid search,
and MCP tool integration via the gobby-code internal server.
"""

from gobby.code_index.hasher import file_content_hash, symbol_content_hash
from gobby.code_index.models import (
    CallRelation,
    ImportRelation,
    IndexedFile,
    IndexedProject,
    IndexResult,
    Symbol,
)
from gobby.code_index.security import is_binary, should_exclude, validate_path

__all__ = [
    "CallRelation",
    "ImportRelation",
    "IndexedFile",
    "IndexedProject",
    "IndexResult",
    "Symbol",
    "file_content_hash",
    "is_binary",
    "should_exclude",
    "symbol_content_hash",
    "validate_path",
]
