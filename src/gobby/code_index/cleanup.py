"""Cleanup helpers for stale code-index projects."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class MissingProject(Protocol):
    id: str
    root_path: str | None


class CodeIndexStorageCleanup(Protocol):
    def delete_project_index(self, project_id: str) -> dict[str, int]: ...


class CodeIndexCleanupConfig(Protocol):
    graph_enabled: bool


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
    project: MissingProject,
    storage: CodeIndexStorageCleanup,
    config: CodeIndexCleanupConfig,
    clear_graph: Callable[[str], Awaitable[dict[str, Any]]] | None,
    run_db: Callable[..., Awaitable[Any]] | None,
) -> None:
    """Remove index data for a project whose root directory is gone."""
    project_id = str(project.id)
    root_path = project.root_path

    if config.graph_enabled and clear_graph is not None:
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
        logger.warning(
            "delete_project_index returned unexpected %s for project %s: %r",
            type(counts).__name__,
            project_id,
            counts,
        )
        raise TypeError(f"delete_project_index returned {type(counts).__name__}, expected dict")

    logger.info(
        "Purged stale code index project %s at %s: %s files, %s symbols",
        project_id,
        root_path,
        counts.get("files", 0),
        counts.get("symbols", 0),
    )
