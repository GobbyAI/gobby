"""Hold ambiguous Claude auto-compaction hooks until their owner is known (#25683).

A Claude subagent shares its parent's Gobby session. When the subagent auto-compacts, its
PreCompact(auto), SessionStart(source=compact) and PostCompact hooks carry the parent's
session id and transcript path, with no agent_id and no subagents/ path, so the subagent
skip cannot see them. The payload cannot tell them apart from the parent's own
auto-compaction, but the transcripts can: a compaction's compact_boundary is written to the
transcript of the session that compacted.

Such hooks are held per session and never reach a handler or a rule. On the parent's next
main-thread event the parent transcript is checked for a new compact_boundary. Found, the
held hooks replay in order through the normal pipeline; missing, they are dropped. A manual
compaction is never held.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.subagent_boundary import subagent_payload_signal

REPLAY_FLAG = "_compaction_settled"
COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"

_AUTO_TRIGGER = "auto"
_COMPACT_SOURCE = "compact"
_SETTLE_SLACK = timedelta(seconds=5)
_READ_CHUNK_BYTES = 1 << 20
_COMPACTION_EVENTS = frozenset(
    {HookEventType.PRE_COMPACT, HookEventType.SESSION_START, HookEventType.POST_COMPACT}
)


@dataclass
class _SessionState:
    last_trigger: str | None = None
    held: list[HookEvent] = field(default_factory=list)
    held_at: datetime | None = None
    transcript_path: str | None = None
    verified_uuid: str | None = None


class CompactionOwnership:
    """Per-session hold queue for auto-compaction hooks whose owner is unknown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _SessionState] = {}

    def hold(self, event: HookEvent) -> bool:
        """Hold an ambiguous compaction hook. True means it must reach no handler or rule."""
        if not _is_unowned_claude_compaction(event):
            return False
        with self._lock:
            state = self._sessions.setdefault(event.session_id, _SessionState())
            if event.event_type == HookEventType.PRE_COMPACT:
                state.last_trigger = str(event.data.get("trigger", _AUTO_TRIGGER))
            elif (
                event.event_type == HookEventType.SESSION_START
                and event.data.get("source") != _COMPACT_SOURCE
            ):
                return False
            if state.last_trigger != _AUTO_TRIGGER:
                return False
            if not state.held:
                state.held_at = datetime.now(UTC)
            transcript = event.data.get("transcript_path")
            if state.transcript_path is None and isinstance(transcript, str):
                state.transcript_path = transcript
            state.held.append(event)
            return True

    def has_held(self, session_id: str) -> bool:
        """Report whether a held batch awaits attribution, before paying for a transcript read."""
        with self._lock:
            state = self._sessions.get(session_id)
            return state is not None and bool(state.held)

    def settle(self, event: HookEvent) -> list[HookEvent]:
        """Attribute the held batch on a main-thread event; return the hooks to replay, in order.

        An empty list means the batch belonged to a subagent (or cannot be attributed) and
        was dropped.
        """
        if event.source != SessionSource.CLAUDE or event.metadata.get(REPLAY_FLAG):
            return []
        if subagent_payload_signal(event.data) is not None:
            return []
        with self._lock:
            state = self._sessions.get(event.session_id)
            if state is None or not state.held:
                return []
            held, held_at, transcript = state.held, state.held_at, state.transcript_path
            verified = state.verified_uuid
            state.held, state.held_at, state.transcript_path = [], None, None
        # The transcript scan runs outside the lock: hold() takes it on the event loop.
        boundary = None
        if held_at is not None and transcript is not None:
            boundary = newest_compact_boundary_since(Path(transcript), held_at - _SETTLE_SLACK)
        if boundary is None or boundary == verified:
            return []
        with self._lock:
            state.verified_uuid = boundary
        for replayed in held:
            replayed.metadata[REPLAY_FLAG] = True
        return held


def newest_compact_boundary_since(
    transcript: Path, not_before: datetime, *, chunk_bytes: int = _READ_CHUNK_BYTES
) -> str | None:
    """Return the uuid of the newest compact_boundary at or after ``not_before``, else ``None``.

    The transcript is read backwards in chunks and the read stops at the first record older
    than the window, so the cost follows the window rather than the transcript's size.
    """
    try:
        with closing(_lines_newest_first(transcript, chunk_bytes)) as lines:
            for raw in lines:
                record = _parse_record(raw)
                if record is None:
                    continue
                stamp = _record_time(record)
                if stamp is not None and stamp < not_before:
                    return None
                if record.get("subtype") == COMPACT_BOUNDARY_SUBTYPE:
                    uuid = record.get("uuid")
                    return uuid if isinstance(uuid, str) else None
    except OSError:
        return None
    return None


def _is_unowned_claude_compaction(event: HookEvent) -> bool:
    return (
        event.source == SessionSource.CLAUDE
        and event.event_type in _COMPACTION_EVENTS
        and not event.metadata.get(REPLAY_FLAG)
        and subagent_payload_signal(event.data) is None
    )


def _lines_newest_first(path: Path, chunk_bytes: int) -> Generator[bytes]:
    """Yield the transcript's lines last-to-first, reading ``chunk_bytes`` at a time."""
    with path.open("rb") as handle:
        position = handle.seek(0, os.SEEK_END)
        carry = b""
        while position > 0:
            size = min(chunk_bytes, position)
            position -= size
            handle.seek(position)
            pieces = (handle.read(size) + carry).split(b"\n")
            # pieces[0] may start mid-line; it joins the earlier chunk on the next pass.
            carry = pieces[0]
            yield from reversed(pieces[1:])
        yield carry


def _parse_record(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _record_time(record: dict[str, Any]) -> datetime | None:
    value = record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
