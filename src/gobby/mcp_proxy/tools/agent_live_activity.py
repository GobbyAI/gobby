"""Live activity overlays for active agent-run status responses."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = {"running", "pending"}


def _count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


async def overlay_live_activity(run: Any, transcript_reader: Any | None) -> Any:
    """Overlay live transcript-derived counters on an active agent run."""
    if transcript_reader is None or run is None:
        return run
    if getattr(run, "status", None) not in _ACTIVE_STATUSES:
        return run

    session_id = getattr(run, "child_session_id", None) or getattr(run, "parent_session_id", None)
    if not session_id:
        return run

    counter = getattr(transcript_reader, "get_activity_counts", None)
    if counter is None:
        return run

    try:
        counts = await counter(session_id)
    except Exception as exc:  # noqa: BLE001 - status reads must stay best-effort
        logger.debug("Failed to read live transcript activity for %s: %s", session_id, exc)
        return run

    tool_calls = _count(counts.get("tool_call_count"))
    turns = _count(counts.get("turn_count"))
    if tool_calls is not None:
        run.tool_calls_count = max(getattr(run, "tool_calls_count", 0) or 0, tool_calls)
    if turns is not None:
        run.turns_used = max(getattr(run, "turns_used", 0) or 0, turns)
    return run


async def overlay_runs_live_activity(runs: list[Any], transcript_reader: Any | None) -> list[Any]:
    for run in runs:
        await overlay_live_activity(run, transcript_reader)
    return runs
