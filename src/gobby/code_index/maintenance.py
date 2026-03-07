"""Background maintenance loop for code indexing.

Periodically walks indexed projects, detects stale files,
and triggers re-indexing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.code_index.indexer import CodeIndexer

logger = logging.getLogger(__name__)


async def code_index_maintenance_loop(
    indexer: CodeIndexer,
    shutdown_flag: asyncio.Event | None = None,
    interval: int = 300,
) -> None:
    """Background loop that checks for stale indexed files.

    Args:
        indexer: CodeIndexer instance.
        shutdown_flag: Event that signals shutdown.
        interval: Seconds between maintenance runs.
    """
    logger.info(f"Code index maintenance loop started (interval={interval}s)")

    while True:
        # Check shutdown
        if shutdown_flag is not None and shutdown_flag.is_set():
            break

        try:
            await _run_maintenance(indexer)
        except Exception as e:
            logger.error(f"Code index maintenance error: {e}", exc_info=True)

        # Wait for interval or shutdown
        if shutdown_flag is not None:
            try:
                await asyncio.wait_for(shutdown_flag.wait(), timeout=interval)
                break  # Shutdown signaled
            except TimeoutError:
                pass  # Normal timeout, loop again
        else:
            await asyncio.sleep(interval)

    logger.info("Code index maintenance loop stopped")


async def _run_maintenance(indexer: CodeIndexer) -> None:
    """Single maintenance pass: re-index stale files in all projects."""
    projects = indexer.storage.list_indexed_projects()

    for project in projects:
        if not project.root_path:
            continue

        try:
            result = await indexer.index_directory(
                root_path=project.root_path,
                project_id=project.id,
                incremental=True,
            )
            if result.files_indexed > 0:
                logger.debug(
                    f"Maintenance reindexed {result.files_indexed} files for project {project.id}"
                )
        except Exception as e:
            logger.warning(f"Maintenance reindex failed for project {project.id}: {e}")
