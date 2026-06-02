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
import json
import logging
import os
import tempfile
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
from gobby.sessions.transcripts.base import ParsedMessage, RawLine, TokenUsage

if TYPE_CHECKING:
    from gobby.sessions.transcripts.base import BaseTranscriptParser

logger = logging.getLogger(__name__)

#: Bounded prefix sample size for content-based source detection.
SOURCE_SAMPLE_LINES = 64
#: Bounded LRU index cache size (entries). Each entry is tens of KB.
INDEX_CACHE_MAX_ENTRIES = 16
INDEX_SCHEMA_VERSION = 1
INDEX_SIDECAR_SUFFIX = ".gobby-index.json"
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
    "clear_index_cache",
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
    parsed_message_count: int
    raw_record_count: int
    source: str
    seek_mode: str  # "byte" | "line"
    mtime_ns: int
    size: int
    tool_first_open: dict[str, int] = field(default_factory=dict)
    post_pass_adjustments: list[RenderedAdjustment] = field(default_factory=list)
    parsed_boundaries: list[ParsedBoundary] = field(default_factory=list)
    role_message_counts: dict[str, int] = field(default_factory=dict)

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
    ) -> None:
        self._parser = parser or _get_parser(
            source, session_id=session_id, transcript_path=transcript_path
        )
        self._session_id = session_id
        self._state = RenderState()
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
            seek_mode=seek_mode,
            mtime_ns=0,
            size=0,
        )

    def append_raw_lines(
        self, raw_lines: Iterable[RawLine], *, mtime_ns: int, size: int
    ) -> TranscriptIndex:
        """Append complete positioned raw lines and update snapshot metadata."""
        raw_counter = [0]
        events = self._parser.iter_parse_events(
            _counting(raw_lines, raw_counter), start_index=self._next_start_index
        )
        for event in events:
            if self._safe_to_start_event and _should_record_parsed_boundary(
                self.index.parsed_boundaries, self.index.parsed_message_count
            ):
                self.index.parsed_boundaries.append(
                    ParsedBoundary(
                        raw_line_start=event.raw_line_no,
                        byte_start=event.byte_offset if self.index.seek_mode == "byte" else None,
                        parsed_index_start=event.parsed_index,
                        message_index_start=self.index.parsed_message_count,
                        role_counts_start=dict(self._role_counts),
                    )
                )

            for offset_in_event, record in enumerate(event.records):
                if not isinstance(record, ParsedMessage):
                    continue
                self.index.parsed_message_count += 1
                self._role_counts[record.role] = self._role_counts.get(record.role, 0) + 1

                if record.content_type in ("tool_use", "mcp_tool_use") and record.tool_use_id:
                    self.index.tool_first_open.setdefault(record.tool_use_id, record.index)

                _completed, self._state = render_incremental(
                    [record],
                    self._state,
                    session_id=self._session_id,
                    error_log=self._parser.error_log,
                )

                current = self._state.current_message
                if current is not None and current.id != self._prev_current_id:
                    self._prev_current_id = current.id
                    self.index.boundaries.append(
                        GroupBoundary(
                            group_index=len(self.index.boundaries),
                            raw_line_start=event.raw_line_no,
                            byte_start=event.byte_offset
                            if self.index.seek_mode == "byte"
                            else None,
                            parsed_index_start=record.index,
                            resume_safe=event.parser_safe and offset_in_event == 0,
                            role=current.role,
                            timestamp=current.timestamp,
                        )
                    )

            if event.parser_safe:
                _free_resolved_tool_calls(self._state)
            self._safe_to_start_event = event.parser_safe
            self._next_start_index = _next_index_after_records(
                event.records, self._next_start_index, event.parsed_index
            )

        self.index.raw_record_count += raw_counter[0]
        self.index.total_groups = len(self.index.boundaries)
        self.index.role_message_counts = dict(self._role_counts)
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
        self._next_raw_line_no += len(line_list)
        return self.append_raw_lines(
            _raw_lines_from_positions(line_list, offset_list, start_line_no),
            mtime_ns=mtime_ns,
            size=size,
        )

    def snapshot(self, *, mtime_ns: int, size: int) -> TranscriptIndex:
        """Return the current index with EOF-dependent parser adjustments resolved."""
        self.index.mtime_ns = mtime_ns
        self.index.size = size
        self.index.total_groups = len(self.index.boundaries)
        self.index.role_message_counts = dict(self._role_counts)
        self.index.post_pass_adjustments = _resolve_adjustments(self._parser, self.index)
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
) -> TranscriptIndex:
    """Drive the real renderer over a raw-line stream, capturing group starts.

    Feeds one parsed message at a time into ``render_incremental`` and records a
    :class:`GroupBoundary` whenever a new ``current_message`` appears. Group
    content is discarded; only positions/counts/suppression data are retained.
    """
    appender = TranscriptIndexAppender(
        source,
        session_id,
        None,
        seek_mode=seek_mode,
        parser=parser,
    )
    appender.append_raw_lines(raw_lines, mtime_ns=mtime_ns, size=size)
    return appender.snapshot(mtime_ns=mtime_ns, size=size)


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


# --------------------------------------------------------------------------- #
# Persistent sidecar store
# --------------------------------------------------------------------------- #


def _sidecar_path(path: str) -> str:
    return f"{os.path.abspath(path)}{INDEX_SIDECAR_SUFFIX}"


def _encode_adjustment_value(value: Any) -> Any:
    if isinstance(value, TokenUsage):
        return {
            "__type__": "TokenUsage",
            "input_tokens": value.input_tokens,
            "output_tokens": value.output_tokens,
            "cache_creation_tokens": value.cache_creation_tokens,
            "cache_read_tokens": value.cache_read_tokens,
        }
    try:
        json.dumps(value)
    except TypeError:
        logger.debug("Skipping non-serializable transcript index adjustment value")
        return None
    return value


def _decode_adjustment_value(value: Any) -> Any:
    if isinstance(value, dict) and value.get("__type__") == "TokenUsage":
        return TokenUsage(
            input_tokens=int(value.get("input_tokens", 0)),
            output_tokens=int(value.get("output_tokens", 0)),
            cache_creation_tokens=int(value.get("cache_creation_tokens", 0)),
            cache_read_tokens=int(value.get("cache_read_tokens", 0)),
        )
    return value


def _index_to_payload(path: str, index: TranscriptIndex) -> dict[str, Any]:
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "source_path": os.path.abspath(path),
        "source": index.source,
        "seek_mode": index.seek_mode,
        "mtime_ns": index.mtime_ns,
        "size": index.size,
        "boundaries": [
            {
                "group_index": boundary.group_index,
                "raw_line_start": boundary.raw_line_start,
                "byte_start": boundary.byte_start,
                "parsed_index_start": boundary.parsed_index_start,
                "resume_safe": boundary.resume_safe,
                "role": boundary.role,
                "timestamp": boundary.timestamp.isoformat(),
            }
            for boundary in index.boundaries
        ],
        "parsed_boundaries": [
            {
                "raw_line_start": boundary.raw_line_start,
                "byte_start": boundary.byte_start,
                "parsed_index_start": boundary.parsed_index_start,
                "message_index_start": boundary.message_index_start,
                "role_counts_start": boundary.role_counts_start,
            }
            for boundary in index.parsed_boundaries
        ],
        "parsed_message_count": index.parsed_message_count,
        "raw_record_count": index.raw_record_count,
        "total_groups": index.total_groups,
        "tool_first_open": index.tool_first_open,
        "role_message_counts": index.role_message_counts,
        "post_pass_adjustments": [
            {
                "group_index": adjustment.group_index,
                "field": adjustment.field,
                "value": _encode_adjustment_value(adjustment.value),
            }
            for adjustment in index.post_pass_adjustments
        ],
    }


def _payload_to_index(payload: dict[str, Any]) -> TranscriptIndex:
    boundaries = [
        GroupBoundary(
            group_index=int(item["group_index"]),
            raw_line_start=int(item["raw_line_start"]),
            byte_start=item.get("byte_start"),
            parsed_index_start=int(item["parsed_index_start"]),
            resume_safe=bool(item["resume_safe"]),
            role=str(item["role"]),
            timestamp=datetime.fromisoformat(str(item["timestamp"])),
        )
        for item in payload.get("boundaries", [])
    ]
    parsed_boundaries = [
        ParsedBoundary(
            raw_line_start=int(item["raw_line_start"]),
            byte_start=item.get("byte_start"),
            parsed_index_start=int(item["parsed_index_start"]),
            message_index_start=int(item["message_index_start"]),
            role_counts_start={
                str(role): int(count)
                for role, count in dict(item.get("role_counts_start", {})).items()
            },
        )
        for item in payload.get("parsed_boundaries", [])
    ]
    adjustments = [
        RenderedAdjustment(
            group_index=int(item["group_index"]),
            field=str(item["field"]),
            value=_decode_adjustment_value(item.get("value")),
        )
        for item in payload.get("post_pass_adjustments", [])
    ]
    return TranscriptIndex(
        boundaries=boundaries,
        total_groups=int(payload["total_groups"]),
        parsed_message_count=int(payload["parsed_message_count"]),
        raw_record_count=int(payload["raw_record_count"]),
        source=str(payload["source"]),
        seek_mode=str(payload["seek_mode"]),
        mtime_ns=int(payload["mtime_ns"]),
        size=int(payload["size"]),
        tool_first_open={
            str(tool_id): int(index)
            for tool_id, index in payload.get("tool_first_open", {}).items()
        },
        post_pass_adjustments=adjustments,
        parsed_boundaries=parsed_boundaries,
        role_message_counts={
            str(role): int(count) for role, count in payload.get("role_message_counts", {}).items()
        },
    )


def _sidecar_matches(
    payload: dict[str, Any],
    *,
    path: str,
    source: str,
    seek_mode: str,
    mtime_ns: int,
    size: int,
) -> bool:
    return (
        payload.get("schema_version") == INDEX_SCHEMA_VERSION
        and payload.get("source_path") == os.path.abspath(path)
        and payload.get("source") == source
        and payload.get("seek_mode") == seek_mode
        and int(payload.get("mtime_ns", -1)) == mtime_ns
        and int(payload.get("size", -1)) == size
    )


def load_index_sidecar(
    path: str, source: str, *, seek_mode: str, mtime_ns: int, size: int
) -> TranscriptIndex | None:
    """Load a sidecar index if it exactly matches the requested snapshot."""
    sidecar = _sidecar_path(path)
    try:
        with open(sidecar, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read transcript index sidecar %s: %s", sidecar, exc)
        return None

    try:
        if not isinstance(payload, dict) or not _sidecar_matches(
            payload, path=path, source=source, seek_mode=seek_mode, mtime_ns=mtime_ns, size=size
        ):
            return None
        return _payload_to_index(payload)
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug("Invalid transcript index sidecar %s: %s", sidecar, exc)
        return None


def persist_index_sidecar(path: str, index: TranscriptIndex) -> None:
    """Atomically persist an index sidecar next to its source transcript."""
    sidecar = _sidecar_path(path)
    directory = os.path.dirname(sidecar)
    os.makedirs(directory, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{os.path.basename(sidecar)}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(_index_to_payload(path, index), handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, sidecar)
    except OSError as exc:
        logger.debug("Failed to persist transcript index sidecar %s: %s", sidecar, exc)
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Bounded async index cache
# --------------------------------------------------------------------------- #

_IndexKey = tuple[str, str, str, int, int]

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

    Keyed by ``(abspath, source, seek_mode, mtime_ns, size)`` so any append
    invalidates the entry. A per-key build lock collapses concurrent first-open
    requests into a single build. ``lines`` selects the line-seek archive build;
    otherwise the file is streamed.
    """
    key: _IndexKey = (os.path.abspath(path), source, seek_mode, mtime_ns, size)

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

        sidecar_index = await asyncio.to_thread(
            load_index_sidecar,
            path,
            source,
            seek_mode=seek_mode,
            mtime_ns=mtime_ns,
            size=size,
        )
        if sidecar_index is not None:
            async with _CACHE_LOCK:
                _INDEX_CACHE[key] = sidecar_index
                _INDEX_CACHE.move_to_end(key)
                while len(_INDEX_CACHE) > INDEX_CACHE_MAX_ENTRIES:
                    _INDEX_CACHE.popitem(last=False)
                _BUILD_LOCKS.pop(key, None)
            return sidecar_index

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
        await asyncio.to_thread(persist_index_sidecar, path, index)

        async with _CACHE_LOCK:
            _INDEX_CACHE[key] = index
            _INDEX_CACHE.move_to_end(key)
            while len(_INDEX_CACHE) > INDEX_CACHE_MAX_ENTRIES:
                _INDEX_CACHE.popitem(last=False)
            _BUILD_LOCKS.pop(key, None)

    return index
