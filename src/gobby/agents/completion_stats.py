"""Resolve agent-run completion counters from durable activity sources."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from gobby.storage.agents import AgentRun

logger = logging.getLogger(__name__)

SESSION_STATS_LOOKUP_TIMEOUT_SECONDS = 2.0


class SessionLookup(Protocol):
    """Session lookup surface needed by completion counter resolution."""

    def get(self, session_id: str) -> object | None: ...


class TranscriptActivityReader(Protocol):
    """Transcript activity surface needed by completion counter resolution."""

    async def get_activity_counts(self, session_id: str) -> Mapping[str, int]: ...


RunDb = Callable[..., Awaitable[Any]]


def _count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(value, 0)
    return 0


def merge_completion_stats(
    run: object,
    *,
    session: object | None = None,
    transcript_counts: Mapping[str, int] | None = None,
) -> tuple[int, int]:
    """Take the maximum tool-call and turn counters from supplied sources."""
    transcript_counts = transcript_counts or {}
    return (
        max(
            _count(getattr(run, "tool_calls_count", 0)),
            _count(getattr(session, "tool_call_count", 0)),
            _count(transcript_counts.get("tool_call_count")),
        ),
        max(
            _count(getattr(run, "turns_used", 0)),
            _count(getattr(session, "turn_count", 0)),
            _count(transcript_counts.get("turn_count")),
        ),
    )


async def resolve_completion_stats(
    run: AgentRun,
    *,
    session: object | None = None,
    session_manager: SessionLookup | None = None,
    transcript_reader: TranscriptActivityReader | None = None,
    run_db: RunDb | None = None,
    session_id: str | None = None,
) -> tuple[int, int]:
    """Resolve completion counters across the run, session row, and transcript."""
    child_session_id = getattr(run, "child_session_id", None)
    if not isinstance(child_session_id, str) or not child_session_id:
        child_session_id = session_id

    resolved_session = session
    if resolved_session is None and child_session_id and session_manager is not None:
        try:
            if run_db is None:
                resolved_session = session_manager.get(child_session_id)
            else:
                resolved_session = await asyncio.wait_for(
                    run_db(session_manager.get, child_session_id),
                    timeout=SESSION_STATS_LOOKUP_TIMEOUT_SECONDS,
                )
        except TimeoutError:
            logger.debug("Timed out reading session stats for agent %s", getattr(run, "id", None))
        except Exception:
            logger.debug(
                "Failed to read session stats for agent %s",
                getattr(run, "id", None),
                exc_info=True,
            )

    transcript_counts: Mapping[str, int] | None = None
    if child_session_id and transcript_reader is not None:
        try:
            transcript_counts = await transcript_reader.get_activity_counts(child_session_id)
        except Exception:
            logger.warning(
                "Failed to read transcript activity for agent %s",
                getattr(run, "id", None),
                exc_info=True,
            )

    return merge_completion_stats(
        run,
        session=resolved_session,
        transcript_counts=transcript_counts,
    )
