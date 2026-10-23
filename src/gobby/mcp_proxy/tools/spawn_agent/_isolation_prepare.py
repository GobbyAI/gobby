"""Timed isolation preparation and its failure cleanup."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.agents.isolation_clone import CloneIsolationHandler
from gobby.agents.isolation_models import IsolationContext, IsolationHandler, SpawnConfig
from gobby.agents.isolation_worktree import WorktreeIsolationHandler
from gobby.agents.spawn_timing import finish_spawn_phase, start_spawn_phase
from gobby.releases.window import ReleaseWindowRefused, require_tree_admissions_open

logger = logging.getLogger(__name__)


def requested_base_branch(explicit: str | None, definition_default: str | None) -> str | None:
    """Preserve omission until the target checkout's isolation handler runs."""
    requested = explicit if explicit is not None else definition_default
    return None if requested == "inherit" else requested


async def prepare_spawn_isolation(
    handler: IsolationHandler, config: SpawnConfig
) -> tuple[IsolationContext | None, dict[str, Any] | None]:
    """Prepare isolation, preserving the spawn boundary's cleanup and error response.

    A fresh worktree or clone is new work, so a release window holds it. Reused trees
    never reach this call; team admission creates its tree under its own window check.
    """
    if isinstance(handler, WorktreeIsolationHandler | CloneIsolationHandler):
        try:
            await require_tree_admissions_open(Path(config.project_path), datetime.now(UTC))
        except ReleaseWindowRefused as exc:
            return None, {"success": False, "error": str(exc), "error_code": exc.code}
    started_at = start_spawn_phase()
    try:
        return await handler.prepare_environment(config), None
    except Exception as exc:
        logger.exception("Failed to prepare environment: %s", exc)
        try:
            await handler.cleanup_environment(config)
        except Exception as cleanup_err:
            logger.warning("Cleanup after prepare failure also failed: %s", cleanup_err)
        response: dict[str, Any] = {
            "success": False,
            "error": f"Failed to prepare environment: {exc}",
        }
        error_code = getattr(exc, "code", None)
        if isinstance(error_code, str):
            response["error_code"] = error_code
        return None, response
    finally:
        finish_spawn_phase(config.phase_timings_ms, "isolation_prepare", started_at)
