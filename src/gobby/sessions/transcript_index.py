"""Indexed transcript windowing — per-session rendered-group boundary index.

Builds a compact map from *rendered group* to source position by streaming the
transcript through the **real** ``render_incremental`` + ``iter_parse_events``
code path, so the recorded boundaries cannot drift from what a full render would
produce. The index lets :mod:`gobby.sessions.transcript_window` seek to a bounded
window of groups without re-rendering the whole transcript, bounding daemon RAM
on very large sessions (the activity-panel crash this work fixes).

Key invariants (see ``docs`` / the approved plan):

* A :class:`GroupBoundary` marks a rendered group's **start**. It is captured
  from the renderer's own output (a new ``current_message`` appearing), never a
  re-implemented "would start a new group" predicate.
* ``parsed_index_start`` is the **global** :attr:`ParsedMessage.index` of the
  message that opens the group, so a windowed render that resumes
  ``iter_parse_events`` with ``start_index=parsed_index_start`` reproduces
  identical ``RenderedMessage.id`` / ``source_line`` values.
* A boundary is ``resume_safe`` iff the opening message is the **first** record
  of a ``parser_safe`` event. Only ``resume_safe`` boundaries are valid seek
  targets — seeking into the middle of a multi-message event would re-parse the
  line from its first message and mis-assign global indices.
* Cross-window tool-result suppression is handled by the window via stub seeding
  driven by :attr:`TranscriptIndex.tool_first_open`, *not* by gating boundaries
  on tool resolution (which would blow the span budget behind a long-running
  call).
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Iterable, Iterator
from copy import copy, deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from gobby.sessions.message_stats import (
    TURN_BOUNDARY_CONTENT_TYPE,
    MessageProtocol,
    MessageStats,
    accumulate_message_stats,
    empty_message_stats,
)
from gobby.sessions.observation_tracker import ObservationTracker
from gobby.sessions.transcript_index_sidecar import (
    _INDEX_CACHE as _INDEX_CACHE,
)
from gobby.sessions.transcript_index_sidecar import (
    INDEX_CACHE_MAX_ENTRIES,
    clear_index_cache,
    discard_index_sidecar,
    get_or_build_index,
    load_index_sidecar,
    persist_index_sidecar,
)
from gobby.sessions.transcript_index_sidecar import (
    INDEX_SCHEMA_VERSION as INDEX_SCHEMA_VERSION,
)
from gobby.sessions.transcript_index_sidecar import (
    INDEX_SIDECAR_SUFFIX as INDEX_SIDECAR_SUFFIX,
)
from gobby.sessions.transcript_index_sidecar import (
    _encode_adjustment_value as _encode_adjustment_value,
)
from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcript_parsing import _get_parser
from gobby.sessions.transcript_renderer import RenderState, render_incremental
from gobby.sessions.transcript_source import (
    _detect_source_from_jsonl_lines,
    _detect_source_from_path,
)
from gobby.sessions.transcripts.base import (
    NON_MESSAGE_CONTENT_TYPES,
    ParsedMessage,
    RawLine,
    TokenUsage,
)

if TYPE_CHECKING:
    from gobby.sessions.transcripts.base import BaseTranscriptParser

logger = logging.getLogger(__name__)

#: Bounded prefix sample size for content-based source detection.
SOURCE_SAMPLE_LINES = 64
PARSED_BOUNDARY_INTERVAL = 128

__all__ = [
    "INDEX_CACHE_MAX_ENTRIES",
    "SOURCE_SAMPLE_LINES",
    "GroupBoundary",
    "ParsedBoundary",
    "RenderedAdjustment",
    "TranscriptIndex",
    "TranscriptIndexAppender",
    "build_index_from_file",
    "build_index_from_lines",
    "build_index_from_raw_lines",
    "clear_index_cache",
    "discard_index_sidecar",
    "detect_source_bounded",
    "get_or_build_index",
    "load_index_sidecar",
    "persist_index_sidecar",
    "rebuild_and_persist_index",
]


@dataclass(slots=True)
class GroupBoundary:
    """Start marker of one rendered group, mapped to a source position.

    A group spans ``[parsed_index_start, next_boundary.parsed_index_start)`` in
    global parsed-message indices (or to EOF for the last group).
    """

    group_index: int
    raw_line_start: int
    byte_start: int | None
    parsed_index_start: int
    resume_safe: bool
    role: str
    timestamp: datetime


@dataclass(slots=True)
class RenderedAdjustment:
    """A post-pass mutation, resolved from a parsed index to its rendered group.

    Produced from a parser's :meth:`finalize` ``ParsedAdjustment`` at index build
    (e.g. Droid sidecar token usage applied to the last assistant message's
    group). The window replays any adjustment whose ``group_index`` falls inside
    the returned window onto that group's :class:`RenderedMessage`.
    """

    group_index: int
    field: str
    value: Any


@dataclass(slots=True)
class ParsedBoundary:
    """Safe raw position for resuming flat ParsedMessage row scans."""

    raw_line_start: int
    byte_start: int | None
    parsed_index_start: int
    message_index_start: int
    role_counts_start: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class TranscriptIndex:
    """Compact, cacheable boundary map for one transcript snapshot."""

    boundaries: list[GroupBoundary]
    total_groups: int
    parsed_message_count: int  # display/flat-row count; excludes session metadata
    raw_record_count: int
    source: str
    session_id: str | None
    seek_mode: str  # "byte" | "line"
    mtime_ns: int
    size: int
    tool_first_open: dict[str, int] = field(default_factory=dict)
    post_pass_adjustments: list[RenderedAdjustment] = field(default_factory=list)
    parsed_boundaries: list[ParsedBoundary] = field(default_factory=list)
    role_message_counts: dict[str, int] = field(default_factory=dict)
    session_stats: MessageStats | None = None
    next_parser_index: int | None = None  # parser resume position; counts every record
    next_raw_line_no: int | None = None
    safe_to_start_event: bool | None = None
    logical_size: int | None = None
    parser_state: dict[str, Any] = field(default_factory=dict)

    def group_index_for_parsed_index(self, parsed_index: int) -> int | None:
        """Return the group_index whose span contains ``parsed_index`` (or None)."""
        starts = [b.parsed_index_start for b in self.boundaries]
        pos = bisect.bisect_right(starts, parsed_index) - 1
        if pos < 0:
            return None
        return self.boundaries[pos].group_index

    def parsed_boundary_for_offset(
        self, offset: int, role: str | None = None
    ) -> ParsedBoundary | None:
        """Return the closest safe flat-row boundary at or before ``offset``."""
        if not self.parsed_boundaries:
            return None
        target = max(0, offset)
        starts = [
            boundary.role_counts_start.get(role, 0) if role else boundary.message_index_start
            for boundary in self.parsed_boundaries
        ]
        pos = bisect.bisect_right(starts, target) - 1
        if pos < 0:
            return self.parsed_boundaries[0]
        return self.parsed_boundaries[pos]


# --------------------------------------------------------------------------- #
# Source detection (bounded — never reads the whole file)
# --------------------------------------------------------------------------- #


def detect_source_bounded(
    path: str,
    *,
    session_source: str | None = None,
    max_lines: int = SOURCE_SAMPLE_LINES,
) -> str:
    """Resolve a transcript source without reading the whole file.

    Tries path-shape detection first; on failure samples a bounded prefix of
    non-blank lines for content detection; falls back to the stored session
    source, then ``"claude"``.
    """
    detected = _detect_source_from_path(path)
    if detected:
        return detected

    sample: list[str] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    sample.append(line)
                    if len(sample) >= max_lines:
                        break
    except OSError as exc:
        logger.debug("Source sample read failed for %s: %s", path, exc)

    detected = _detect_source_from_jsonl_lines(sample)
    return detected or session_source or "claude"


# --------------------------------------------------------------------------- #
# Raw-line streaming
# --------------------------------------------------------------------------- #


def _iter_file_raw_lines(path: str, size: int) -> Iterator[RawLine]:
    """Stream a JSONL file as positioned :class:`RawLine`s up to ``size`` bytes.

    Byte offsets are the start byte of each line, so the window can ``seek`` to a
    boundary later. Reading stops once a line's start reaches the snapshot size,
    so concurrent appends to a live transcript can't extend this build.
    """
    offset = 0
    line_no = 0
    with open(path, "rb") as handle:
        for raw_bytes in handle:
            if offset >= size:
                break
            text = raw_bytes.decode("utf-8", errors="replace")
            yield RawLine(byte_offset=offset, raw_line_no=line_no, text=text)
            offset += len(raw_bytes)
            line_no += 1


def _iter_text_raw_lines(lines: Iterable[str]) -> Iterator[RawLine]:
    """Wrap decompressed archive lines as positionless (line-mode) RawLines."""
    for i, text in enumerate(lines):
        yield RawLine(byte_offset=None, raw_line_no=i, text=text)


def _counting(raws: Iterable[RawLine], counter: list[int]) -> Iterator[RawLine]:
    """Pass-through that counts non-blank raw records consumed by the parser.

    Matches ``_count_nonempty_lines`` semantics used by the status/count paths,
    including lines the parser later drops (e.g. Claude's skip partner, which is
    still consumed from the source iterator).
    """
    for raw in raws:
        if raw.text.strip():
            counter[0] += 1
        yield raw


def _counting_with_line_cursor(
    raws: Iterable[RawLine],
    nonempty_counter: list[int],
    next_line_no: list[int],
) -> Iterator[RawLine]:
    for raw in raws:
        next_line_no[0] = max(next_line_no[0], raw.raw_line_no + 1)
        if raw.text.strip():
            nonempty_counter[0] += 1
        yield raw


# --------------------------------------------------------------------------- #
# Index build core
# --------------------------------------------------------------------------- #


def _raw_lines_from_positions(
    lines: Iterable[str], byte_offsets: Iterable[int], start_line_no: int
) -> Iterator[RawLine]:
    """Wrap already-tailed complete text lines with their source byte offsets."""
    for offset, line in zip(byte_offsets, lines, strict=True):
        yield RawLine(byte_offset=offset, raw_line_no=start_line_no, text=line)
        start_line_no += 1


def _should_record_parsed_boundary(boundaries: list[ParsedBoundary], message_count: int) -> bool:
    if not boundaries:
        return True
    return message_count - boundaries[-1].message_index_start >= PARSED_BOUNDARY_INTERVAL


def _next_index_after_records(records: list[Any], fallback: int, parsed_index: int) -> int:
    next_index = fallback
    for record in records:
        if isinstance(record, ParsedMessage):
            next_index = max(next_index, record.index + 1)
    if not records:
        next_index = max(next_index, parsed_index + 1)
    return next_index


class TranscriptIndexAppender:
    """Incrementally maintain a :class:`TranscriptIndex` from tailed raw lines."""

    def __init__(
        self,
        source: str,
        session_id: str | None,
        transcript_path: str | None,
        *,
        seek_mode: str = "byte",
        parser: BaseTranscriptParser | None = None,
        observation_tracker: ObservationTracker | None = None,
    ) -> None:
        self._parser = parser or _get_parser(
            source, session_id=session_id, transcript_path=transcript_path
        )
        self._session_id = session_id
        self._state = RenderState()
        self._observation_tracker = observation_tracker or ObservationTracker()
        self._role_counts: dict[str, int] = {}
        self._prev_current_id: str | None = None
        self._next_start_index = 0
        self._next_raw_line_no = 0
        self._safe_to_start_event = True
        self.index = TranscriptIndex(
            boundaries=[],
            total_groups=0,
            parsed_message_count=0,
            raw_record_count=0,
            source=source,
            session_id=session_id,
            seek_mode=seek_mode,
            mtime_ns=0,
            size=0,
            session_stats=empty_message_stats(),
            next_parser_index=0,
            next_raw_line_no=0,
            safe_to_start_event=True,
        )

    def clone(self) -> TranscriptIndexAppender:
        """Copy mutable indexing state while sharing the observation sink."""
        cloned = copy(self)
        cloned._parser = deepcopy(self._parser)
        cloned._state = deepcopy(self._state)
        cloned._role_counts = dict(self._role_counts)
        cloned.index = deepcopy(self.index)
        return cloned

    def append_raw_lines(
        self, raw_lines: Iterable[RawLine], *, mtime_ns: int, size: int
    ) -> TranscriptIndex:
        """Append complete positioned raw lines and update snapshot metadata."""
        raw_counter = [0]
        next_raw_line_no = [self._next_raw_line_no]
        stats_messages: list[ParsedMessage] = []
        events = self._parser.iter_parse_events(
            _counting_with_line_cursor(raw_lines, raw_counter, next_raw_line_no),
            start_index=self._next_start_index,
        )
        for event in events:
            records = normalize_transcript_records(event.records, self.index.source)
            if self._safe_to_start_event and _should_record_parsed_boundary(
                self.index.parsed_boundaries, self.index.parsed_message_count
            ):
                self.index.parsed_boundaries.append(
                    ParsedBoundary(
                        raw_line_start=event.raw_line_no,
                        byte_start=event.byte_offset
                        if _stores_byte_offsets(self.index.seek_mode)
                        else None,
                        parsed_index_start=event.parsed_index,
                        message_index_start=self.index.parsed_message_count,
                        role_counts_start=dict(self._role_counts),
                    )
                )

            for offset_in_event, record in enumerate(records):
                if not isinstance(record, ParsedMessage):
                    continue

                # Session metadata (native titles, unmodeled-record sentinel) is
                # excluded from display/flat counters, but still advances parser
                # position via _next_start_index / event.parsed_index below.
                # render_incremental runs unconditionally so the unmodeled-record
                # sentinel is still observed (and metadata produces no group).
                # Turn-boundary records join NON_MESSAGE_CONTENT_TYPES so they
                # stay out of parsed_message_count/role counts, but they must
                # still reach accumulate_message_stats to increment turn_count.
                if record.content_type == TURN_BOUNDARY_CONTENT_TYPE:
                    stats_messages.append(record)
                elif record.content_type not in NON_MESSAGE_CONTENT_TYPES:
                    stats_messages.append(record)
                    self.index.parsed_message_count += 1
                    self._role_counts[record.role] = self._role_counts.get(record.role, 0) + 1

                    if record.content_type in ("tool_use", "mcp_tool_use") and record.tool_use_id:
                        self.index.tool_first_open.setdefault(record.tool_use_id, record.index)

                _completed, self._state = render_incremental(
                    [record],
                    self._state,
                    session_id=self._session_id,
                    error_log=self._parser.error_log,
                    source=self.index.source,
                    observation_tracker=self._observation_tracker,
                )

                current = self._state.current_message
                if current is not None and current.id != self._prev_current_id:
                    self._prev_current_id = current.id
                    self.index.boundaries.append(
                        GroupBoundary(
                            group_index=len(self.index.boundaries),
                            raw_line_start=event.raw_line_no,
                            byte_start=event.byte_offset
                            if _stores_byte_offsets(self.index.seek_mode)
                            else None,
                            parsed_index_start=record.index,
                            resume_safe=event.parser_safe and offset_in_event == 0,
                            role=current.role,
                            timestamp=current.timestamp,
                        )
                    )

            self._safe_to_start_event = event.parser_safe
            self._next_start_index = _next_index_after_records(
                records, self._next_start_index, event.parsed_index
            )

        if stats_messages:
            self.index.session_stats = accumulate_message_stats(
                self.index.session_stats, cast("list[MessageProtocol]", stats_messages)
            )
        self.index.raw_record_count += raw_counter[0]
        self._next_raw_line_no = max(self._next_raw_line_no, next_raw_line_no[0])
        self.index.total_groups = len(self.index.boundaries)
        self.index.role_message_counts = dict(self._role_counts)
        self.index.next_parser_index = self._next_start_index
        self.index.next_raw_line_no = self._next_raw_line_no
        self.index.safe_to_start_event = self._safe_to_start_event
        self.index.mtime_ns = mtime_ns
        self.index.size = size
        return self.index

    def append_positioned_lines(
        self, lines: Iterable[str], byte_offsets: Iterable[int], *, mtime_ns: int, size: int
    ) -> TranscriptIndex:
        """Append complete text lines already read by the live tailer."""
        line_list = list(lines)
        offset_list = list(byte_offsets)
        start_line_no = self._next_raw_line_no
        return self.append_raw_lines(
            _raw_lines_from_positions(line_list, offset_list, start_line_no),
            mtime_ns=mtime_ns,
            size=size,
        )

    def hydrate_from_index(
        self,
        *,
        index: TranscriptIndex,
        state: RenderState,
        current_id: str | None,
        next_parser_index: int,
        next_raw_line_no: int,
    ) -> TranscriptIndexAppender:
        self.index, self._state = index, state
        self._role_counts = dict(index.role_message_counts)
        self._prev_current_id = current_id
        self._next_start_index, self._next_raw_line_no = next_parser_index, next_raw_line_no
        self._safe_to_start_event = index.safe_to_start_event is not False
        self._parser.hydrate_state(index.parser_state)
        return self

    def snapshot(self, *, mtime_ns: int, size: int) -> TranscriptIndex:
        """Return the current index with EOF-dependent parser adjustments resolved."""
        self.index.mtime_ns = mtime_ns
        self.index.size = size
        self.index.total_groups = len(self.index.boundaries)
        self.index.role_message_counts = dict(self._role_counts)
        self.index.next_parser_index = self._next_start_index
        self.index.next_raw_line_no = self._next_raw_line_no
        self.index.safe_to_start_event = self._safe_to_start_event
        new_adjustments = _resolve_adjustments(self._parser, self.index)
        self.index.parser_state = self._parser.snapshot_state()
        self.index.post_pass_adjustments = _merge_adjustments(
            self.index.post_pass_adjustments, new_adjustments
        )
        return self.index


def _build_index_core(
    raw_lines: Iterable[RawLine],
    parser: BaseTranscriptParser,
    *,
    source: str,
    seek_mode: str,
    session_id: str | None,
    mtime_ns: int,
    size: int,
    logical_size: int | None = None,
) -> TranscriptIndex:
    """Drive the real renderer over a raw-line stream, capturing group starts.

    Feeds one parsed message at a time into ``render_incremental`` and records a
    :class:`GroupBoundary` whenever a new ``current_message`` appears. Group
    content is discarded; only positions/counts/suppression data are retained.
    """
    _require_gzip_logical_size(seek_mode, logical_size)
    appender = TranscriptIndexAppender(
        source,
        session_id,
        None,
        seek_mode=seek_mode,
        parser=parser,
    )
    appender.append_raw_lines(raw_lines, mtime_ns=mtime_ns, size=size)
    index = appender.snapshot(mtime_ns=mtime_ns, size=size)
    index.logical_size = logical_size
    return index


def _stores_byte_offsets(seek_mode: str) -> bool:
    return seek_mode in {"byte", "gzip-block"}


def _require_gzip_logical_size(seek_mode: str, logical_size: int | None) -> None:
    if seek_mode == "gzip-block" and logical_size is None:
        raise ValueError("logical_size is required for gzip-block transcript indexes")


def _resolve_adjustments(
    parser: BaseTranscriptParser, index: TranscriptIndex
) -> list[RenderedAdjustment]:
    """Resolve ``finalize()`` parsed-index adjustments to rendered-group ones."""
    resolved: list[RenderedAdjustment] = []
    for adjustment in parser.finalize():
        group_index = index.group_index_for_parsed_index(adjustment.parsed_index)
        if group_index is None:
            continue
        resolved.append(
            RenderedAdjustment(
                group_index=group_index,
                field=adjustment.field,
                value=adjustment.value,
            )
        )
    return resolved


def _merge_adjustments(
    existing: list[RenderedAdjustment], new: list[RenderedAdjustment]
) -> list[RenderedAdjustment]:
    merged = list(existing)
    for adjustment in new:
        for index, previous in enumerate(merged):
            if previous.group_index != adjustment.group_index or previous.field != adjustment.field:
                continue
            if isinstance(previous.value, TokenUsage) and isinstance(adjustment.value, TokenUsage):
                merged[index] = RenderedAdjustment(
                    group_index=adjustment.group_index,
                    field=adjustment.field,
                    value=TokenUsage(
                        input_tokens=previous.value.input_tokens + adjustment.value.input_tokens,
                        output_tokens=previous.value.output_tokens + adjustment.value.output_tokens,
                        cache_creation_tokens=previous.value.cache_creation_tokens
                        + adjustment.value.cache_creation_tokens,
                        cache_read_tokens=previous.value.cache_read_tokens
                        + adjustment.value.cache_read_tokens,
                    ),
                )
            else:
                merged[index] = adjustment
            break
        else:
            merged.append(adjustment)
    return merged


def build_index_from_file(
    path: str,
    source: str,
    session_id: str | None,
    *,
    mtime_ns: int,
    size: int,
) -> TranscriptIndex:
    """Build a byte-seekable index by streaming a live JSONL transcript file."""
    parser = _get_parser(source, session_id=session_id, transcript_path=path)
    return _build_index_core(
        _iter_file_raw_lines(path, size),
        parser,
        source=source,
        seek_mode="byte",
        session_id=session_id,
        mtime_ns=mtime_ns,
        size=size,
    )


def build_index_from_lines(
    lines: Iterable[str],
    source: str,
    session_id: str | None,
    *,
    mtime_ns: int,
    size: int,
    transcript_path: str | None = None,
) -> TranscriptIndex:
    """Build a line-seekable index from decompressed archive lines.

    Archives can't be byte-seeked cheaply, so boundaries carry only
    ``raw_line_start`` (``seek_mode="line"``) and the window stream-skips to them.
    """
    parser = _get_parser(source, session_id=session_id, transcript_path=transcript_path)
    return _build_index_core(
        _iter_text_raw_lines(lines),
        parser,
        source=source,
        seek_mode="line",
        session_id=session_id,
        mtime_ns=mtime_ns,
        size=size,
    )


def build_index_from_raw_lines(
    raw_lines: Iterable[RawLine],
    source: str,
    session_id: str | None,
    *,
    seek_mode: str,
    mtime_ns: int,
    size: int,
    transcript_path: str | None = None,
    logical_size: int | None = None,
) -> TranscriptIndex:
    """Build an index from a caller-owned positioned raw-line stream."""
    _require_gzip_logical_size(seek_mode, logical_size)
    parser = _get_parser(source, session_id=session_id, transcript_path=transcript_path)
    return _build_index_core(
        raw_lines,
        parser,
        source=source,
        seek_mode=seek_mode,
        session_id=session_id,
        mtime_ns=mtime_ns,
        size=size,
        logical_size=logical_size,
    )


def rebuild_and_persist_index(
    path: str,
    source: str,
    session_id: str | None,
    *,
    mtime_ns: int,
    size: int,
) -> TranscriptIndex:
    """Rebuild a byte-seek index and atomically persist its sidecar."""
    index = build_index_from_file(path, source, session_id, mtime_ns=mtime_ns, size=size)
    persist_index_sidecar(path, index)
    return index
