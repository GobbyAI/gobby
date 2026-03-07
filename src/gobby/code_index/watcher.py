"""Integration triggers for code indexing.

Handles git post-commit hooks and session start triggers
that notify the indexer of changed files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.code_index.indexer import CodeIndexer

logger = logging.getLogger(__name__)


async def handle_incremental_index(
    indexer: CodeIndexer,
    project_id: str,
    root_path: str,
    changed_files: list[str],
) -> dict[str, int]:
    """Handle incremental index trigger (e.g., from git post-commit).

    Args:
        indexer: CodeIndexer instance.
        project_id: Project to reindex.
        root_path: Project root path.
        changed_files: List of changed file paths (relative to root).

    Returns:
        Dict with files_indexed and symbols_found counts.
    """
    if not changed_files:
        return {"files_indexed": 0, "symbols_found": 0, "files_skipped": 0, "duration_ms": 0}

    result = await indexer.index_changed_files(
        project_id=project_id,
        root_path=root_path,
        file_paths=changed_files,
    )

    logger.info(
        f"Incremental index: {result.files_indexed} files, "
        f"{result.symbols_found} symbols in {result.duration_ms}ms"
    )

    return {
        "files_indexed": result.files_indexed,
        "symbols_found": result.symbols_found,
        "files_skipped": getattr(result, "files_skipped", 0),
        "duration_ms": result.duration_ms,
    }


async def handle_session_start_index(
    indexer: CodeIndexer,
    project_id: str,
    root_path: str,
) -> dict[str, int]:
    """Handle session start auto-indexing.

    Runs a full incremental index of the project.

    Args:
        indexer: CodeIndexer instance.
        project_id: Project to index.
        root_path: Project root path.

    Returns:
        Dict with indexing stats.
    """
    root = Path(root_path)
    if not root.is_dir():
        logger.warning(f"Session start index: not a directory: {root_path}")
        return {"files_indexed": 0, "symbols_found": 0, "files_skipped": 0, "duration_ms": 0}

    result = await indexer.index_directory(
        root_path=root_path,
        project_id=project_id,
        incremental=True,
    )

    logger.info(
        f"Session start index: {result.files_indexed} files "
        f"({result.files_skipped} unchanged) in {result.duration_ms}ms"
    )

    return {
        "files_indexed": result.files_indexed,
        "files_skipped": result.files_skipped,
        "symbols_found": result.symbols_found,
        "duration_ms": result.duration_ms,
    }
