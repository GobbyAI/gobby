"""Call-time sync point for provider transcripts that flush behind live activity.

A provider can record activity in a live sidecar long before those records reach the
transcript the close path parses. Grok is the extreme case: a headless run is a single
turn, and ``updates.jsonl`` only carries that turn's tool calls once the turn ends,
while ``events.jsonl`` beside it gains a ``tool_completed`` record as each call
finishes. Close evidence derived before that flush credits nothing the worker ran
(#22367), so the close path compares what it parsed against this sync point and waits
for the transcript to catch up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gobby.sessions.transcript_cursor import GrokEventsCursor, TranscriptObservationError
from gobby.storage.session_models import Session

#: Sidecar records that have a counterpart in the parsed transcript. Lifecycle
#: chatter (phase changes, permission prompts) never reaches the transcript, so
#: including it would leave the comparison permanently behind.
_SYNCED_RECORD_TYPES = frozenset({"tool_completed", "turn_ended"})


def transcript_sync_point(session: Session) -> datetime | None:
    """Return the newest live provider activity the parsed transcript must contain.

    ``None`` means there is no independent signal to compare against — the provider
    publishes none, or its sidecar is unreadable — so whatever the transcript holds is
    the whole truth available and the close path must not wait on it.
    """
    if session.source != "grok":
        return None
    try:
        records = GrokEventsCursor.beside_transcript(session.transcript_path).tail_records()
    except TranscriptObservationError:
        return None
    stamps = [
        stamp
        for record in records
        if str(record.get("type")) in _SYNCED_RECORD_TYPES
        for stamp in (_record_timestamp(record),)
        if stamp is not None
    ]
    return max(stamps) if stamps else None


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    raw = record.get("ts") or record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
