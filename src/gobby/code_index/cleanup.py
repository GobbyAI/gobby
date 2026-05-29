"""Cleanup helpers for stale code-index projects."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


async def _run_db(
    run_db: Callable[..., Awaitable[Any]] | None,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    if run_db is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    return await run_db(func, *args, **kwargs)


async def purge_missing_project(
    *,
    project: Any,
    storage: Any,
    config: Any,
    vector_store: Any | None,
    clear_graph: Callable[[str], Awaitable[dict[str, Any]]] | None,
    run_db: Callable[..., Awaitable[Any]] | None,
) -> None:
    """Remove index data for a project whose root directory is gone."""
    project_id = str(project.id)
    root_path = getattr(project, "root_path", None)

    if getattr(config, "graph_enabled", True) and clear_graph is not None:
        try:
            result = await clear_graph(project_id)
            if not result.get("success", False):
                logger.warning(
                    "Graph cleanup reported failure for missing code index project %s: %s",
                    project_id,
                    result.get("error", "unknown error"),
                )
        except Exception as e:
            logger.warning(
                "Graph cleanup failed for missing code index project %s: %s",
                project_id,
                e,
                exc_info=True,
            )

    counts = await _run_db(run_db, storage.delete_project_index, project_id)
    if not isinstance(counts, dict):
        counts = {}

    if vector_store is not None:
        collection = f"{config.qdrant_collection_prefix}{project_id}"
        try:
            await vector_store.delete_collection(collection)
        except Exception as e:
            logger.warning(
                "Vector cleanup failed for missing code index project %s: %s",
                project_id,
                e,
                exc_info=True,
            )

    logger.info(
        "Purged stale code index project %s at %s: %s files, %s symbols",
        project_id,
        root_path,
        counts.get("files", 0),
        counts.get("symbols", 0),
    )
