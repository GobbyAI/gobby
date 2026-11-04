"""Attribute Claude auto-compaction hooks to the parent or a subagent (#25683).

A Claude subagent shares its parent's Gobby session. Its auto-compaction fires PreCompact(auto),
SessionStart(source=compact) and PostCompact with the parent's session id and transcript path,
and no agent_id or subagents/ path, so the subagent skip cannot see them. The transcripts can:
a compaction writes its compact_boundary to the transcript of the session that compacted, and a
subagent's transcript lives under <session>/subagents/.

- SessionStart(compact) and PostCompact after an auto PreCompact are decided on arrival by the
  newest compact_boundary at or before that arrival, across the parent and its subagents. A
  parent boundary passes them to the handlers and releases the PreCompact that it completes. A
  subagent boundary, or none, holds them. A compaction writes its boundary just before its own
  tail hooks, so the nearest boundary identifies the owner even when compactions overlap.
- PreCompact(auto) is held until a boundary claims it. It replays only when it is the one
  pending PreCompact that a parent boundary can belong to. Otherwise it is dropped, so
  overlapping compactions fail closed. Their PreCompact cannot be told apart from the payload.

Manual compactions are never held. A held event reaches no handler or rule.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.subagent_boundary import subagent_payload_signal

REPLAY_FLAG = "_compaction_settled"
COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"

_AUTO = "auto"
_MANUAL = "manual"
_COMPACT_SOURCE = "compact"
_SLACK = timedelta(seconds=5)
_TAIL_WINDOW = timedelta(seconds=60)
_PENDING_TTL = timedelta(minutes=15)
_READ_CHUNK_BYTES = 1 << 20


@dataclass(frozen=True)
class _Boundary:
    uuid: str
    at: datetime
    trigger: str | None
    parent: bool

    @property
    def auto(self) -> bool:
        return self.trigger != _MANUAL


@dataclass
class _Pending:
    event: HookEvent
    at: datetime
    transcript: Path | None


@dataclass
class _SessionState:
    last_trigger: str | None = None
    pending: list[_Pending] = field(default_factory=list)
    claimed: set[str] = field(default_factory=set)


class CompactionOwnership:
    """Per-session attribution of Claude auto-compaction hooks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _SessionState] = {}

    def needs_transcripts(self, event: HookEvent) -> bool:
        """Report whether route() may read transcripts, so async callers run it off the loop."""
        if not _is_claude_main(event):
            return False
        if event.event_type == HookEventType.PRE_COMPACT or _is_tail(event):
            return True
        with self._lock:
            state = self._sessions.get(event.session_id)
            return state is not None and bool(state.pending)

    def route(self, event: HookEvent) -> tuple[list[HookEvent], bool]:
        """Return the held PreCompacts to replay before ``event``, and whether ``event`` is held.

        A held event must reach no handler or rule; the caller answers allow.
        """
        if not _is_claude_main(event):
            return [], False
        now = datetime.now(UTC)
        with self._lock:
            state = self._sessions.get(event.session_id)
            if state is None:
                if event.event_type != HookEventType.PRE_COMPACT:
                    return [], False
                state = self._sessions.setdefault(event.session_id, _SessionState())
            if event.event_type == HookEventType.PRE_COMPACT:
                return self._pre_compact(state, event, now)
            if _is_tail(event):
                return self._tail(state, event, now)
            return self._resolve(state, now), False

    def _pre_compact(
        self, state: _SessionState, event: HookEvent, now: datetime
    ) -> tuple[list[HookEvent], bool]:
        replays = self._resolve(state, now)
        trigger = str(event.data.get("trigger", _AUTO))
        state.last_trigger = trigger
        if trigger != _AUTO:
            return replays, False
        state.pending.append(_Pending(event, now, _transcript_of(event)))
        return replays, True

    def _tail(
        self, state: _SessionState, event: HookEvent, now: datetime
    ) -> tuple[list[HookEvent], bool]:
        if state.last_trigger != _AUTO:
            return [], False
        boundary = _nearest_boundary(_transcript_of(event), now)
        if boundary is None:
            return [], True
        state.claimed.add(boundary.uuid)
        candidates = [p for p in state.pending if p.at <= boundary.at + _SLACK]
        dropped = {id(p) for p in candidates}
        state.pending = [p for p in state.pending if id(p) not in dropped]
        if not boundary.parent:
            return [], True
        replays = [_settled(candidates[0].event)] if len(candidates) == 1 else []
        return replays, False

    def _resolve(self, state: _SessionState, now: datetime) -> list[HookEvent]:
        """Claim the earliest unclaimed auto boundary for the pending PreCompacts it can match."""
        state.pending = [p for p in state.pending if now - p.at <= _PENDING_TTL]
        if not state.pending:
            return []
        oldest = state.pending[0]
        if oldest.transcript is None:
            return []
        found = [
            b
            for b in _boundaries_since(oldest.transcript, oldest.at - _SLACK)
            if b.auto and b.uuid not in state.claimed and b.at <= now + _SLACK
        ]
        if not found:
            return []
        boundary = found[0]
        state.claimed.add(boundary.uuid)
        candidates = [p for p in state.pending if p.at <= boundary.at + _SLACK]
        dropped = {id(p) for p in candidates}
        state.pending = [p for p in state.pending if id(p) not in dropped]
        if boundary.parent and len(candidates) == 1:
            return [_settled(candidates[0].event)]
        return []


def _nearest_boundary(transcript: Path | None, now: datetime) -> _Boundary | None:
    if transcript is None:
        return None
    near = [b for b in _boundaries_since(transcript, now - _TAIL_WINDOW) if b.at <= now + _SLACK]
    return near[-1] if near else None


def _boundaries_since(transcript: Path, not_before: datetime) -> list[_Boundary]:
    """Every compact_boundary since ``not_before`` in the parent and its subagents, oldest first."""
    found: list[_Boundary] = []
    for path in (transcript, *_subagent_transcripts(transcript, not_before)):
        try:
            records = _boundary_records(path, not_before, _READ_CHUNK_BYTES)
        except OSError:
            continue
        found.extend(
            _Boundary(uuid, at, trigger, path == transcript) for uuid, at, trigger in records
        )
    return sorted(found, key=lambda boundary: boundary.at)


def _subagent_transcripts(transcript: Path, not_before: datetime) -> list[Path]:
    """Subagent transcripts of this session written at or after ``not_before``."""
    root = transcript.with_suffix("") / "subagents"
    try:
        candidates = list(root.rglob("*.jsonl"))
    except OSError:
        return []
    cutoff = not_before.timestamp()
    recent: list[Path] = []
    for path in candidates:
        try:
            if path.stat().st_mtime >= cutoff:
                recent.append(path)
        except OSError:
            continue
    return recent


def _boundary_records(
    path: Path, not_before: datetime, chunk_bytes: int
) -> list[tuple[str, datetime, str | None]]:
    """compact_boundary records at or after ``not_before`` in one transcript, oldest first.

    The transcript is read backwards and the read stops at the first record older than the
    window, so the cost follows the window rather than the transcript's size.
    """
    records: list[tuple[str, datetime, str | None]] = []
    with closing(_lines_newest_first(path, chunk_bytes)) as lines:
        for raw in lines:
            record = _parse_record(raw)
            if record is None:
                continue
            stamp = _record_time(record)
            if stamp is None:
                continue
            if stamp < not_before:
                break
            uuid = record.get("uuid")
            if record.get("subtype") == COMPACT_BOUNDARY_SUBTYPE and isinstance(uuid, str):
                records.append((uuid, stamp, _trigger_of(record)))
    records.reverse()
    return records


def _settled(event: HookEvent) -> HookEvent:
    """Copy a held PreCompact marked so route() passes it to the rules instead of holding it."""
    return replace(event, metadata={**event.metadata, REPLAY_FLAG: True})


def _is_claude_main(event: HookEvent) -> bool:
    return (
        event.source == SessionSource.CLAUDE
        and not event.metadata.get(REPLAY_FLAG)
        and subagent_payload_signal(event.data) is None
    )


def _is_tail(event: HookEvent) -> bool:
    return event.event_type == HookEventType.POST_COMPACT or (
        event.event_type == HookEventType.SESSION_START
        and event.data.get("source") == _COMPACT_SOURCE
    )


def _transcript_of(event: HookEvent) -> Path | None:
    value = event.data.get("transcript_path")
    return Path(value) if isinstance(value, str) and value else None


def _trigger_of(record: dict[str, Any]) -> str | None:
    metadata = record.get("compactMetadata")
    trigger = metadata.get("trigger") if isinstance(metadata, dict) else None
    return trigger if isinstance(trigger, str) else None


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
