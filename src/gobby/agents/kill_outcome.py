"""What a kill_agent outcome lets a caller do with the run row."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from gobby.agents.kill import KILL_ERROR_NO_TARGET_PID

logger = logging.getLogger(__name__)


def kill_confirmed(result: Mapping[str, Any]) -> bool:
    """A kill proved the run stopped or found no target PID; other outcomes leave the run alone."""
    return bool(result.get("success")) or result.get("error_code") == KILL_ERROR_NO_TARGET_PID


def may_cancel_run(result: Mapping[str, Any] | None) -> bool:
    """Proceed unless a kill returned an unconfirmed outcome; a raised kill proceeds."""
    return result is None or kill_confirmed(result)


def reconcile_cancelled_run(manager: Any, run_id: str) -> None:
    """Persist cancellation after a process kill, retrying a transient manager failure."""
    try:
        cancelled = manager.cancel(run_id)
    except Exception:
        logger.warning(
            "Failed to cancel agent run '%s'; retrying reconciliation",
            run_id,
            exc_info=True,
        )
        cancelled = manager.cancel(run_id)

    if cancelled is None:
        current = manager.get(run_id)
        if current is not None and current.status in ("running", "pending"):
            raise RuntimeError(f"Agent run '{run_id}' remained active after cancellation")
