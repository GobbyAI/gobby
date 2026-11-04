"""Hold Claude auto-compaction hooks until the parent's own compaction settles (#25683).

A Claude subagent shares its parent's Gobby session. Its auto-compaction fires PreCompact,
SessionStart(source=compact) and PostCompact with the parent's session id and transcript path,
and no agent_id or subagents/ path, so the subagent skip cannot classify them. They are held
per session, and their payloads cannot tell the parent from a subagent:

- An auto PreCompact, a SessionStart(source=compact) and a PostCompact are held. A manual
  compaction runs immediately, after any held hooks settle, and its boundary is verified at
  its tails. A held PreCompact is not replayed when a manual boundary settles, because the
  manual PreCompact already ran its rules.
- The next non-compaction main-thread event settles the held hooks. It counts the
  compact_boundary records in the parent's main transcript since the last verified boundary.
  None drops every held hook. One or more runs one parent lifecycle, built from the latest held
  payload of each kind, and records the newest boundary as verified. The lifecycle's response
  context is merged into the settling event's response.
- Subagent transcripts and timestamps are never read.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.subagent_boundary import subagent_payload_signal

REPLAY_FLAG = "_compaction_settled"
COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"

_AUTO = "auto"
_MANUAL = "manual"
_COMPACT_SOURCE = "compact"
_BOUNDARY_MARKER = b'"compact_boundary"'
_READ_CHUNK_BYTES = 1 << 20

_PRE = "pre_compact"
_START = "session_start"
_POST = "post_compact"
_LIFECYCLE = (_PRE, _START, _POST)


@dataclass
class _SessionState:
    last_trigger: str | None = None
    held: dict[str, HookEvent] = field(default_factory=dict)
    verified: str | None = None
    # A manual PreCompact ran its rules for the boundary that has not been verified yet.
    manual_pre_ran: bool = False


class CompactionOwnership:
    """Per-session holding and settling of Claude auto-compaction hooks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _SessionState] = {}

    def needs_transcripts(self, event: HookEvent) -> bool:
        """Report whether route() may read the parent transcript, for async callers."""
        if not _is_claude_main(event):
            return False
        if _is_compaction(event):
            return True
        with self._lock:
            state = self._sessions.get(event.session_id)
            return state is not None and bool(state.held)

    def route(self, event: HookEvent) -> tuple[list[HookEvent], bool]:
        """Return the lifecycle replays to run before ``event``, and whether ``event`` is held.

        A held event must reach no handler or rule; the caller answers allow. A replay carries
        REPLAY_FLAG, so route() passes it through instead of holding it again.
        """
        if not _is_claude_main(event):
            return [], False
        with self._lock:
            if _is_compaction(event):
                return self._hold(event)
            state = self._sessions.get(event.session_id)
            if state is None or not state.held:
                return [], False
            return self._settle(state, event), False

    def _hold(self, event: HookEvent) -> tuple[list[HookEvent], bool]:
        state = self._sessions.get(event.session_id)
        if state is None:
            # Boundaries written before the session's first compaction hook belong to earlier
            # compactions. Its PreCompact runs before the boundary it must count, so this baseline
            # is taken before that boundary is written.
            state = _SessionState(verified=_newest_boundary(_transcript_of(event)))
            self._sessions[event.session_id] = state
        if event.event_type == HookEventType.PRE_COMPACT:
            trigger = str(event.data.get("trigger", _AUTO))
            state.last_trigger = trigger
            if trigger != _AUTO:
                # The manual boundary is written after this PreCompact, so settling now counts
                # only earlier boundaries.
                replays = self._settle(state, event) if state.held else []
                state.manual_pre_ran = True
                return replays, False
            kind = _PRE
        else:
            if state.last_trigger == _MANUAL:
                # The manual boundary is written before its tails. Verify it so that no later
                # settle counts it again.
                boundary = _newest_boundary(_transcript_of(event))
                if boundary is not None:
                    state.verified = boundary
                state.manual_pre_ran = False
                return [], False
            kind = _START if event.event_type == HookEventType.SESSION_START else _POST
        state.held[kind] = event
        return [], True

    def _settle(self, state: _SessionState, event: HookEvent) -> list[HookEvent]:
        # A manual PreCompact already ran its rules for the boundary being settled.
        lifecycle = [
            state.held[kind]
            for kind in _LIFECYCLE
            if kind in state.held and not (kind == _PRE and state.manual_pre_ran)
        ]
        state.held.clear()
        state.manual_pre_ran = False
        transcript = _transcript_of(event)
        if transcript is None:
            return []
        try:
            fresh = _boundaries_newest_first(transcript, state.verified, limit=None)
        except OSError:
            return []
        if not fresh:
            return []
        state.verified = fresh[0]
        return [_settled(held) for held in lifecycle]


def merge_replayed_context(response: HookResponse, replayed: list[HookResponse]) -> HookResponse:
    """Put the context that settled lifecycle replays produced into the settling response."""
    parts = [part for result in replayed for part in result.context_contributors()]
    if parts:
        response.add_context(*parts, prepend=True)
    return response


def _newest_boundary(transcript: Path | None) -> str | None:
    if transcript is None:
        return None
    try:
        found = _boundaries_newest_first(transcript, None, limit=1)
    except OSError:
        return None
    return found[0] if found else None


def _boundaries_newest_first(transcript: Path, stop_at: str | None, limit: int | None) -> list[str]:
    """compact_boundary uuids in the parent transcript, newest first, down to ``stop_at``.

    ``stop_at`` is excluded. The transcript is read backwards, so a settle costs the bytes
    written since the last verified boundary.
    """
    found: list[str] = []
    with closing(_lines_newest_first(transcript, _READ_CHUNK_BYTES)) as lines:
        for raw in lines:
            if _BOUNDARY_MARKER not in raw:
                continue
            record = _parse_record(raw)
            if record is None or record.get("subtype") != COMPACT_BOUNDARY_SUBTYPE:
                continue
            uuid = record.get("uuid")
            if not isinstance(uuid, str):
                continue
            if uuid == stop_at:
                break
            found.append(uuid)
            if limit is not None and len(found) >= limit:
                break
    return found


def _settled(event: HookEvent) -> HookEvent:
    """Copy a held hook marked so route() passes it to the rules instead of holding it."""
    return replace(event, metadata={**event.metadata, REPLAY_FLAG: True})


def _is_claude_main(event: HookEvent) -> bool:
    return (
        event.source == SessionSource.CLAUDE
        and not event.metadata.get(REPLAY_FLAG)
        and subagent_payload_signal(event.data) is None
    )


def _is_compaction(event: HookEvent) -> bool:
    if event.event_type in (HookEventType.PRE_COMPACT, HookEventType.POST_COMPACT):
        return True
    return event.event_type == HookEventType.SESSION_START and (
        event.data.get("source") == _COMPACT_SOURCE
    )


def _transcript_of(event: HookEvent) -> Path | None:
    value = event.data.get("transcript_path")
    return Path(value) if isinstance(value, str) and value else None


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
