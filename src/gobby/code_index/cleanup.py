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
    run_db: Callable[..., Awaitable[Any]] | None,
) -> None:
    """Remove this machine's state for a project whose local root is gone."""
    project_id = str(project.id)
    root_path = project.root_path

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
        "Removed stale local code index state for project %s at %s",
        project_id,
        root_path,
    )
