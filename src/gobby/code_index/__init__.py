"""Code index daemon integration for Gobby.

Models, storage, and daemon-side context. Parsing, hashing, and
indexing are handled by gcode (Rust CLI).
"""

from gobby.code_index.context import CodeIndexContext
from gobby.code_index.models import (
    CallRelation,
    ImportRelation,
    IndexedFile,
    IndexedProject,
    IndexResult,
    Symbol,
    make_external_symbol_id,
    make_unresolved_callee_id,
)

__all__ = [
    "CallRelation",
    "CodeIndexContext",
    "ImportRelation",
    "IndexedFile",
    "IndexedProject",
    "IndexResult",
    "Symbol",
    "make_external_symbol_id",
    "make_unresolved_callee_id",
]
