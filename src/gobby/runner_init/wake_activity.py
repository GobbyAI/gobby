"""Shared terminal activity capture for wake dispatch and restart recovery."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.sessions.transcript_cursor import TranscriptObservationError, build_turn_settled_observer
from gobby.terminals.composer_ledger import read_composer

logger = logging.getLogger(__name__)

# In-flight evidence from the transcript rather than the pane: its last turn is open.
TRANSCRIPT_TURN_OPEN = "transcript-turn-open"


async def probe_terminal_activity(session: Any, terminal: Any | None) -> TerminalActivity:
    """Read the composer from the ledger and an open turn from the transcript.

    Nothing reads the screen. A provider without transcript turn records, or a
    transcript that cannot be read, offers no in-flight evidence, so the ledger
    and the session's reconciled status decide alone.
    """
    if terminal is None:
        return TerminalActivity(ComposerRead("unknown"))
    open_turn = await asyncio.to_thread(_transcript_turn_open, session)
    return TerminalActivity(
        composer=read_composer(str(terminal.id)),
        turn_in_flight_fingerprint=TRANSCRIPT_TURN_OPEN if open_turn else None,
    )


def _transcript_turn_open(session: Any) -> bool:
    session_id = getattr(session, "id", None)
    try:
        observer = build_turn_settled_observer(
            getattr(session, "source", None),
            getattr(session, "transcript_path", None),
            session_id=session_id,
        )
    except TranscriptObservationError:
        logger.debug("No transcript turn state for session %s", session_id, exc_info=True)
        return False
    return observer is not None and observer() is False
