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

import asyncio
import bisect
import logging
import os
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from gobby.sessions.transcript_parsing import _get_parser
from gobby.sessions.transcript_renderer import RenderState, render_incremental
from gobby.sessions.transcript_source import (
    _detect_source_from_jsonl_lines,
    _detect_source_from_path,
)
from gobby.sessions.transcripts.base import ParsedMessage, RawLine

if TYPE_CHECKING:
    from gobby.sessions.transcripts.base import BaseTranscriptParser

logger = logging.getLogger(__name__)

#: Bounded prefix sample size for content-based source detection.
SOURCE_SAMPLE_LINES = 64
#: Bounded LRU index cache size (entries). Each entry is tens of KB.
INDEX_CACHE_MAX_ENTRIES = 16

__all__ = [
    "INDEX_CACHE_MAX_ENTRIES",
    "SOURCE_SAMPLE_LINES",
    "GroupBoundary",
    "RenderedAdjustment",
    "TranscriptIndex",
    "build_index_from_file",
    "build_index_from_lines",
    "clear_index_cache",
    "detect_source_bounded",
    "get_or_build_index",
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
class TranscriptIndex:
    """Compact, cacheable boundary map for one transcript snapshot."""

    boundaries: list[GroupBoundary]
    total_groups: int
    parsed_message_count: int
    raw_record_count: int
    source: str
    seek_mode: str  # "byte" | "line"
    mtime_ns: int
    size: int
    tool_first_open: dict[str, int] = field(default_factory=dict)
    post_pass_adjustments: list[RenderedAdjustment] = field(default_factory=list)

    def group_index_for_parsed_index(self, parsed_index: int) -> int | None:
        """Return the group_index whose span contains ``parsed_index`` (or None)."""
        starts = [b.parsed_index_start for b in self.boundaries]
        pos = bisect.bisect_right(starts, parsed_index) - 1
        if pos < 0:
            return None
        return self.boundaries[pos].group_index


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


# --------------------------------------------------------------------------- #
# Index build core
# --------------------------------------------------------------------------- #


def _build_index_core(
    raw_lines: Iterable[RawLine],
    parser: BaseTranscriptParser,
    *,
    source: str,
    seek_mode: str,
    session_id: str | None,
    mtime_ns: int,
    size: int,
) -> TranscriptIndex:
    """Drive the real renderer over a raw-line stream, capturing group starts.

    Feeds one parsed message at a time into ``render_incremental`` and records a
    :class:`GroupBoundary` whenever a new ``current_message`` appears. Group
    content is discarded; only positions/counts/suppression data are retained.
    """
    state = RenderState()
    boundaries: list[GroupBoundary] = []
    tool_first_open: dict[str, int] = {}
    raw_counter = [0]
    parsed_message_count = 0
    prev_current_id: str | None = None

    for event in parser.iter_parse_events(_counting(raw_lines, raw_counter), start_index=0):
        for offset_in_event, record in enumerate(event.records):
            if not isinstance(record, ParsedMessage):
                continue
            parsed_message_count += 1

            if record.content_type in ("tool_use", "mcp_tool_use") and record.tool_use_id:
                tool_first_open.setdefault(record.tool_use_id, record.index)

            # Advance the real renderer one message at a time; discard groups.
            _completed, state = render_incremental(
                [record], state, session_id=session_id, error_log=parser.error_log
            )

            # A fresh current_message means this record opened a new rendered
            # group. group_index == its ordinal == its position in `boundaries`
            # (groups start, complete, and render in the same order).
            current = state.current_message
            if current is not None and current.id != prev_current_id:
                prev_current_id = current.id
                boundaries.append(
                    GroupBoundary(
                        group_index=len(boundaries),
                        raw_line_start=event.raw_line_no,
                        byte_start=event.byte_offset if seek_mode == "byte" else None,
                        parsed_index_start=record.index,
                        resume_safe=event.parser_safe and offset_in_event == 0,
                        role=current.role,
                        timestamp=current.timestamp,
                    )
                )

        if event.parser_safe:
            _free_resolved_tool_calls(state)

    index = TranscriptIndex(
        boundaries=boundaries,
        total_groups=len(boundaries),
        parsed_message_count=parsed_message_count,
        raw_record_count=raw_counter[0],
        source=source,
        seek_mode=seek_mode,
        mtime_ns=mtime_ns,
        size=size,
        tool_first_open=tool_first_open,
    )
    index.post_pass_adjustments = _resolve_adjustments(parser, index)
    return index


def _free_resolved_tool_calls(state: RenderState) -> None:
    """Drop heavy fields of resolved pending tool calls, keeping their keys.

    Bounds index-build RAM to roughly one inter-boundary span: the only reason
    the index build retains ``pending_tool_calls`` is the membership check that
    suppresses duplicate tool_results, so the large argument/result payloads can
    be released once a call is resolved.
    """
    for call in state.pending_tool_calls.values():
        if call.status == "completed":
            call.arguments = {}
            call.result = None


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


# --------------------------------------------------------------------------- #
# Bounded async index cache
# --------------------------------------------------------------------------- #

_IndexKey = tuple[str, str, int, int]

_INDEX_CACHE: OrderedDict[_IndexKey, TranscriptIndex] = OrderedDict()
_CACHE_LOCK = asyncio.Lock()
_BUILD_LOCKS: dict[_IndexKey, asyncio.Lock] = {}


def clear_index_cache() -> None:
    """Drop all cached indexes (invalidation / tests)."""
    _INDEX_CACHE.clear()
    _BUILD_LOCKS.clear()


async def get_or_build_index(
    path: str,
    source: str,
    session_id: str | None,
    *,
    seek_mode: str = "byte",
    lines: Iterable[str] | None = None,
    mtime_ns: int,
    size: int,
) -> TranscriptIndex:
    """Return a cached index for the snapshot, building once off the event loop.

    Keyed by ``(abspath, source, mtime_ns, size)`` so any append (which changes
    mtime/size) invalidates the entry. A per-key build lock collapses concurrent
    first-open requests into a single build. ``lines`` (decompressed archive
    content) selects the line-seek archive build; otherwise the file is streamed.
    """
    key: _IndexKey = (os.path.abspath(path), source, mtime_ns, size)

    async with _CACHE_LOCK:
        cached = _INDEX_CACHE.get(key)
        if cached is not None:
            _INDEX_CACHE.move_to_end(key)
            return cached
        build_lock = _BUILD_LOCKS.setdefault(key, asyncio.Lock())

    async with build_lock:
        async with _CACHE_LOCK:
            cached = _INDEX_CACHE.get(key)
            if cached is not None:
                _INDEX_CACHE.move_to_end(key)
                return cached

        if lines is not None:
            materialized = list(lines)
            index = await asyncio.to_thread(
                build_index_from_lines,
                materialized,
                source,
                session_id,
                mtime_ns=mtime_ns,
                size=size,
                transcript_path=path,
            )
        else:
            index = await asyncio.to_thread(
                build_index_from_file,
                path,
                source,
                session_id,
                mtime_ns=mtime_ns,
                size=size,
            )

        async with _CACHE_LOCK:
            _INDEX_CACHE[key] = index
            _INDEX_CACHE.move_to_end(key)
            while len(_INDEX_CACHE) > INDEX_CACHE_MAX_ENTRIES:
                _INDEX_CACHE.popitem(last=False)
            _BUILD_LOCKS.pop(key, None)

    return index
