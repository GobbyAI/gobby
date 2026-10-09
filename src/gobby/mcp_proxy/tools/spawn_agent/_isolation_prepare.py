"""Timed isolation preparation and its failure cleanup."""

from __future__ import annotations

import logging
from typing import Any

from gobby.agents.isolation_models import IsolationContext, IsolationHandler, SpawnConfig
from gobby.agents.spawn_timing import finish_spawn_phase, start_spawn_phase

logger = logging.getLogger(__name__)


async def prepare_spawn_isolation(
    handler: IsolationHandler, config: SpawnConfig
) -> tuple[IsolationContext | None, dict[str, Any] | None]:
    """Prepare isolation, preserving the spawn boundary's cleanup and error response."""
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
