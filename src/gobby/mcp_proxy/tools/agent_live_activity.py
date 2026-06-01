"""Transcript activity overlays for agent-run status responses."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Mapping
from typing import Any, Protocol, overload

logger = logging.getLogger(__name__)


class LiveActivityRun(Protocol):
    @property
    def child_session_id(self) -> str | None: ...

    @property
    def parent_session_id(self) -> str | None: ...

    tool_calls_count: int
    turns_used: int


class TranscriptReader(Protocol):
    def get_activity_counts(self, session_id: str) -> Awaitable[Mapping[str, Any]]: ...


def _count(value: Any) -> int | None:
    """Return a non-negative integer count, or None when the value is absent/invalid."""
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


@overload
async def overlay_live_activity[RunT: LiveActivityRun](
    run: RunT,
    transcript_reader: TranscriptReader | None,
) -> RunT: ...


@overload
async def overlay_live_activity(
    run: None,
    transcript_reader: TranscriptReader | None,
) -> None: ...


async def overlay_live_activity[RunT: LiveActivityRun](
    run: RunT | None,
    transcript_reader: TranscriptReader | None,
) -> RunT | None:
    """Overlay transcript-derived counters on an agent run."""
    if transcript_reader is None or run is None:
        return run

    session_id: str | None = None
    try:
        session_id = run.child_session_id or run.parent_session_id
        if not session_id:
            return run

        counter = getattr(transcript_reader, "get_activity_counts", None)
        if counter is None:
            return run

        counts = await counter(session_id)
        tool_calls = _count(counts.get("tool_call_count"))
        turns = _count(counts.get("turn_count"))
        if tool_calls is not None:
            run.tool_calls_count = max(getattr(run, "tool_calls_count", 0) or 0, tool_calls)
        if turns is not None:
            run.turns_used = max(getattr(run, "turns_used", 0) or 0, turns)
    except (AttributeError, TypeError, KeyError, ValueError) as exc:
        logger.debug("Failed to read live transcript activity for %s: %s", session_id, exc)
        return run
    except Exception:
        logger.warning("Unexpected live transcript activity overlay failure", exc_info=True)
        return run
    return run


async def overlay_runs_live_activity[RunT: LiveActivityRun](
    runs: list[RunT],
    transcript_reader: TranscriptReader | None,
) -> list[RunT]:
    results = await asyncio.gather(
        *(overlay_live_activity(run, transcript_reader) for run in runs),
        return_exceptions=False,
    )
    return [run for run in results if run is not None]
