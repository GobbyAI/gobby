"""Hold Claude compaction hooks until the parent's own compaction settles (#25683).

A Claude subagent shares its parent's Gobby session. Its auto-compaction fires PreCompact,
SessionStart(source=compact) and PostCompact with the parent's session id and transcript path,
and no agent_id, so the payloads cannot tell the parent from a subagent. A subagent's boundary
is written to its own transcript, never to the parent's.

- An auto PreCompact, a SessionStart(source=compact) and a PostCompact are held per session.
  A manual PreCompact runs its rules at once. Its boundary, like every parent compaction's,
  settles at the next main-thread event.
- The next non-compaction main-thread event settles the held hooks. It counts the
  compact_boundary records in the parent's main transcript since the last verified boundary.
  None drops every held hook, so a session_start(compact) alone never resets. One or more runs
  one parent lifecycle, built from the latest held payload of each kind, and records the newest
  boundary as verified. A held PreCompact is skipped when a manual PreCompact ran since the last
  settle. The lifecycle's response context is merged into the settling event's response. An
  unreadable transcript leaves the held hooks and their state unchanged, so the next
  main-thread event tries again.
- Subagent transcripts are never read. Parent timestamps are read only for an unknown baseline:
  when the session's first compaction hook cannot read the transcript, a boundary counts as new
  when its timestamp is at or after that hook's millisecond.
- Each route decision is logged with its hook, trigger, source and signal.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.subagent_boundary import subagent_payload_signal

logger = logging.getLogger(__name__)

REPLAY_FLAG = "_compaction_settled"
COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"

_AUTO = "auto"
_COMPACT_SOURCE = "compact"
_BOUNDARY_MARKER = b'"compact_boundary"'
_READ_CHUNK_BYTES = 1 << 20

_PRE = "pre_compact"
_START = "session_start"
_POST = "post_compact"
_LIFECYCLE = (_PRE, _START, _POST)


@dataclass
class _SessionState:
    held: dict[str, HookEvent] = field(default_factory=dict)
    verified: str | None = None
    # Set while the baseline is unknown: the receive time of the session's first compaction hook,
    # floored to its millisecond. Parent boundaries written at or after it are new.
    unknown_since: datetime | None = None
    # A manual PreCompact ran its rules since the last settle; a held PreCompact is then skipped.
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
            replays = self._settle(state, event)
            return ([] if replays is None else replays), False

    def _hold(self, event: HookEvent) -> tuple[list[HookEvent], bool]:
        state = self._sessions.get(event.session_id)
        if state is None:
            state = _baseline(event)
            self._sessions[event.session_id] = state
        if event.event_type == HookEventType.PRE_COMPACT:
            if str(event.data.get("trigger", _AUTO)) != _AUTO:
                # The manual PreCompact runs its rules now. Its boundary settles at the next
                # main-thread event, which also skips any PreCompact held before it.
                state.manual_pre_ran = True
                _log_route("manual", event, "manual_pre_compact")
                return [], False
            kind = _PRE
        elif event.event_type == HookEventType.SESSION_START:
            kind = _START
        else:
            kind = _POST
        state.held[kind] = event
        _log_route("held", event, "awaiting_main_event")
        return [], True

    def _settle(self, state: _SessionState, event: HookEvent) -> list[HookEvent] | None:
        """Settle the held lifecycle; None when the transcript cannot be read, which changes nothing.

        With a known baseline, the boundaries newer than it count. With an unknown one, the
        boundaries written at or after its hook count.
        """
        transcript = _transcript_of(event)
        if transcript is None:
            _log_route("kept", event, "no_transcript")
            return None
        try:
            if state.unknown_since is None:
                fresh = _boundaries_newest_first(transcript, state.verified, limit=None)
                newest = fresh[0] if fresh else state.verified
            else:
                fresh, newest = _boundaries_since(transcript, state.unknown_since)
        except OSError:
            _log_route("kept", event, "unreadable_transcript")
            return None
        kinds = [kind for kind in _LIFECYCLE if kind in state.held]
        # A manual PreCompact already ran its rules for the boundary being settled.
        lifecycle = [
            state.held[kind] for kind in kinds if not (kind == _PRE and state.manual_pre_ran)
        ]
        state.held.clear()
        state.manual_pre_ran = False
        state.verified = newest
        state.unknown_since = None
        held = ",".join(kinds)
        if not fresh:
            _log_route("dropped", event, "no_parent_boundary", f" held={held}")
            return []
        _log_route(
            "settled",
            event,
            "parent_boundary",
            f" boundaries={len(fresh)} held={held} replays={len(lifecycle)}",
        )
        return [_settled(held_event) for held_event in lifecycle]


def merge_replayed_context(response: HookResponse, replayed: list[HookResponse]) -> HookResponse:
    """Put the context that settled lifecycle replays produced into the settling response."""
    parts = [part for result in replayed for part in result.context_contributors()]
    if parts:
        response.add_context(*parts, prepend=True)
    return response


def _baseline(event: HookEvent) -> _SessionState:
    """The state for a session's first compaction hook.

    Boundaries written before that hook belong to earlier compactions. Its PreCompact runs before
    the boundary it must count, so the baseline is read before that boundary is written. When the
    transcript cannot be read, the baseline is unknown, and the boundaries written from this hook
    on are the new ones.
    """
    try:
        return _SessionState(verified=_newest_boundary(_transcript_of(event)))
    except OSError:
        return _SessionState(unknown_since=_receive_time())


def _receive_time() -> datetime:
    """Now, floored to the millisecond, the precision of transcript timestamps.

    A boundary written in this millisecond counts as new. The floor can count an older boundary
    from the same millisecond as new, which repeats one reset and never drops a new one.
    """
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def _newest_boundary(transcript: Path | None) -> str | None:
    """The newest parent boundary; raises OSError when the transcript cannot be read."""
    if transcript is None:
        raise FileNotFoundError("the event carries no transcript path")
    found = _boundaries_newest_first(transcript, None, limit=1)
    return found[0] if found else None


def _boundary_records(transcript: Path) -> Generator[tuple[str, dict[str, Any]]]:
    """(uuid, record) for each parent compact_boundary, newest first."""
    with closing(_lines_newest_first(transcript, _READ_CHUNK_BYTES)) as lines:
        for raw in lines:
            if _BOUNDARY_MARKER not in raw:
                continue
            record = _parse_record(raw)
            if record is None or record.get("subtype") != COMPACT_BOUNDARY_SUBTYPE:
                continue
            uuid = record.get("uuid")
            if isinstance(uuid, str):
                yield uuid, record


def _boundaries_newest_first(transcript: Path, stop_at: str | None, limit: int | None) -> list[str]:
    """compact_boundary uuids in the parent transcript, newest first, down to ``stop_at``.

    ``stop_at`` is excluded. The transcript is read backwards, so a settle costs the bytes
    written since the last verified boundary.
    """
    found: list[str] = []
    with closing(_boundary_records(transcript)) as records:
        for uuid, _record in records:
            if uuid == stop_at:
                break
            found.append(uuid)
            if limit is not None and len(found) >= limit:
                break
    return found


def _boundaries_since(transcript: Path, since: datetime) -> tuple[list[str], str | None]:
    """Boundaries written at or after ``since``, newest first, and the newest boundary overall.

    Transcript order is write order, so the scan stops at the first boundary written before
    ``since``. A boundary without a readable timestamp counts as written at or after ``since``.
    """
    fresh: list[str] = []
    newest: str | None = None
    with closing(_boundary_records(transcript)) as records:
        for uuid, record in records:
            if newest is None:
                newest = uuid
            if _written_before(record, since):
                break
            fresh.append(uuid)
    return fresh, newest


def _written_before(record: dict[str, Any], since: datetime) -> bool:
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return False
    try:
        written = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return written.tzinfo is not None and written < since


def _settled(event: HookEvent) -> HookEvent:
    """Copy a held hook marked so route() passes it to the rules instead of holding it."""
    return replace(event, metadata={**event.metadata, REPLAY_FLAG: True})


def _log_route(decision: str, event: HookEvent, signal: str, detail: str = "") -> None:
    """Log one route decision with the hook, trigger, source and signal that produced it."""
    logger.info(
        "compaction route %s: hook=%s trigger=%s source=%s signal=%s%s session=%s",
        decision,
        event.event_type.value,
        event.data.get("trigger", "-"),
        event.data.get("source", "-"),
        signal,
        detail,
        event.session_id,
    )


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
