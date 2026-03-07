"""PostToolUse compressor for Read output.

Replaces large Read outputs with symbol outlines for indexed files,
enabling dramatic token savings while preserving discoverability.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.code_index.models import Symbol
from gobby.code_index.storage import CodeIndexStorage

if TYPE_CHECKING:
    from gobby.config.code_index import CodeIndexConfig

logger = logging.getLogger(__name__)

# Number of lines from the start of the file to include
_HEAD_LINES = 50

# Minimum output length (chars) before compression triggers
_MIN_OUTPUT_LENGTH = 20000


@dataclass
class CompressResult:
    """Result of a compression operation."""

    compressed: str
    original_chars: int
    compressed_chars: int
    symbols_shown: int

    @property
    def savings_pct(self) -> float:
        if self.original_chars == 0:
            return 0.0
        return round((1 - self.compressed_chars / self.original_chars) * 100, 1)


class CodeIndexCompressor:
    """Replaces large Read outputs with symbol outlines for indexed files."""

    def __init__(
        self,
        storage: CodeIndexStorage,
        config: CodeIndexConfig | None = None,
    ) -> None:
        self._storage = storage
        self._min_length = _MIN_OUTPUT_LENGTH
        if config and hasattr(config, "max_file_size_bytes"):
            # Use a fraction of max_file_size as compression threshold
            self._min_length = min(_MIN_OUTPUT_LENGTH, config.max_file_size_bytes // 5)

    def compress_read_output(
        self,
        file_path: str,
        original_output: str,
        project_id: str,
    ) -> CompressResult | None:
        """Replace Read output with symbol outline if file is indexed.

        Returns None if file is not indexed or output is too small.
        """
        if len(original_output) < self._min_length:
            return None

        # Try to find the file in the index
        # Normalize path: try relative variants
        symbols = self._find_symbols(file_path, project_id)
        if not symbols:
            return None

        # Build the compressed output
        lines = original_output.split("\n")
        total_lines = len(lines)

        # Include first N lines for immediate context
        head = "\n".join(lines[:_HEAD_LINES])

        # Build symbol outline
        outline = self._build_outline(symbols)

        compressed = (
            f"# File: {file_path} ({total_lines} lines, indexed)\n"
            f"# First {min(_HEAD_LINES, total_lines)} lines shown, then symbol outline.\n"
            f"# Use get_symbol(id) for full source of any symbol.\n\n"
            f"{head}\n\n"
            f"## Symbol Outline ({len(symbols)} symbols)\n\n"
            f"{outline}\n\n"
            f"## Retrieval\n"
            f'  call_tool("gobby-code", "get_symbol", {{"symbol_id": "<id>"}})\n'
            f'  call_tool("gobby-code", "search_symbols", {{"query": "..."}})\n'
        )

        return CompressResult(
            compressed=compressed,
            original_chars=len(original_output),
            compressed_chars=len(compressed),
            symbols_shown=len(symbols),
        )

    def _find_symbols(self, file_path: str, project_id: str) -> list[Symbol]:
        """Find symbols for a file, trying path variants."""
        # Try exact path
        symbols = self._storage.get_symbols_for_file(project_id, file_path)
        if symbols:
            return symbols

        # Try just the filename
        symbols = self._storage.search_symbols_by_name(
            query="", project_id=project_id, file_path=file_path, limit=200
        )
        if symbols:
            return symbols

        # Try relative path variants
        parts = Path(file_path).parts
        for i in range(len(parts)):
            candidate = str(Path(*parts[i:]))
            symbols = self._storage.get_symbols_for_file(project_id, candidate)
            if symbols:
                return symbols

        return []

    def _build_outline(self, symbols: list[Symbol]) -> str:
        """Build indented symbol outline."""
        lines: list[str] = []
        for sym in symbols:
            indent = "    " if sym.parent_symbol_id else "  "
            kind_prefix = sym.kind
            sig = sym.signature or sym.name
            if len(sig) > 80:
                sig = sig[:77] + "..."
            lines.append(
                f"{indent}{kind_prefix} {sym.name:<30s} "
                f"[L{sym.line_start}-L{sym.line_end}]  "
                f"id: {sym.id[:8]}  sig: {sig}"
            )
        return "\n".join(lines)
