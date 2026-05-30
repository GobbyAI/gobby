"""Windowed transcript rendering over a cached boundary index.

Renders a bounded slice of rendered groups ``[g_start, g_end)`` without
re-rendering the whole transcript, by resuming ``iter_parse_events`` +
``render_incremental`` from a nearby ``resume_safe`` boundary recorded in the
:class:`~gobby.sessions.transcript_index.TranscriptIndex`. Daemon RAM per request
is bounded by the page plus a capped clean-extension span (``max_span``), so very
large sessions (the activity-panel crash this work fixes) no longer materialize
the full render.

Correctness rests on three index-provided facts:

* ``resume_safe`` boundaries are the only valid seek targets — resuming there with
  ``start_index = parsed_index_start`` reproduces identical global parsed indices,
  so ``RenderedMessage.id`` / ``source_line`` match a full render exactly. A group
  that begins mid-event is reconstructed by rendering forward from the preceding
  ``resume_safe`` boundary, never seeked to directly.
* ``tool_first_open`` lets the window seed stub ``pending_tool_calls`` for every
  tool opened before the window start, so a duplicate / cross-window
  ``tool_result`` is suppressed (paired into a throwaway stub) instead of emitting
  an orphan group — matching ``render_transcript``.
* ``post_pass_adjustments`` (e.g. Droid sidecar token usage) are resolved to
  rendered groups at index build (which sees the true EOF) and replayed here onto
  any group that falls inside the window.

Paging composes by ``returned_count`` (never by the requested ``limit``): under
the ``max_span`` budget a page may shrink, and the kept edge is always the one
adjacent to already-loaded content — the **suffix** ``[g_end - returned_count :
g_end)`` for ``order="tail"`` and the **prefix** ``[g_start : g_start +
returned_count)`` for ``order="head"`` — so a caller advancing ``offset`` by
``returned_count`` never gaps or overlaps.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gobby.sessions.transcript_parsing import _get_parser
from gobby.sessions.transcript_renderer import (
    RenderedMessage,
    RenderedToolCall,
    RenderState,
    render_incremental,
)
from gobby.sessions.transcripts.base import ParsedMessage, RawLine

if TYPE_CHECKING:
    from gobby.sessions.transcript_index import GroupBoundary, TranscriptIndex

logger = logging.getLogger(__name__)

#: Hard RAM ceiling for one window render: lookback + page + clean forward
#: extension. When the *forward extension* would exceed this, the response is
#: marked degraded and the dangling in-window tool call stays ``pending``.
MAX_WINDOW_SPAN_BYTES = 8 * 1024 * 1024

__all__ = [
    "MAX_WINDOW_SPAN_BYTES",
    "WindowResult",
    "render_window",
]


@dataclass(slots=True)
class WindowResult:
    """One rendered window plus the metadata callers page on.

    ``returned_count`` is the authoritative page size — callers advance their
    offset by it, not by the requested ``limit`` (a degraded/short page is
    shorter). ``total_groups`` is the rendered-group pagination total, while
    ``parsed_message_count`` is the parsed-message total used only for the
    "N messages" display (different unit — never page on it).
    """

    groups: list[RenderedMessage]
    returned_count: int
    total_groups: int
    parsed_message_count: int = 0
    degraded: bool = False
    degraded_reason: str | None = None
    boundaries_used: int = field(default=0, repr=False)


# --------------------------------------------------------------------------- #
# Range selection
# --------------------------------------------------------------------------- #


def _requested_range(total: int, limit: int, offset: int, order: str) -> tuple[int, int]:
    """Resolve the requested ``[g_start, g_end)`` group range (oldest-first)."""
    if order == "tail":
        end = max(0, total - offset)
        start = max(0, end - limit)
        return start, end
    start = min(offset, total)
    end = min(offset + limit, total)
    return start, end


def _byte_start(boundary: GroupBoundary) -> int:
    """Byte offset of a boundary (only valid for ``seek_mode="byte"`` indexes)."""
    if boundary.byte_start is None:  # pragma: no cover - guarded by caller
        raise ValueError("byte selection requires a byte-seek index")
    return boundary.byte_start


def _resume_group(index: TranscriptIndex, g_start: int) -> int:
    """Largest ``resume_safe`` group_index ``<= g_start`` (0 as a safe floor)."""
    for j in range(min(g_start, len(index.boundaries) - 1), -1, -1):
        if index.boundaries[j].resume_safe:
            return j
    return 0


def _select_range(
    index: TranscriptIndex, g_start_req: int, g_end_req: int, order: str, max_span: int
) -> tuple[int, int]:
    """Shrink the page to fit ``max_span`` while keeping the composable edge.

    ``order="tail"`` keeps the newer edge (``g_end`` fixed, advance ``g_start``);
    ``order="head"`` keeps the older edge (``g_start`` fixed, retract ``g_end``).
    A single group larger than the budget is returned alone (never sub-split).
    Line-seek indexes have no per-boundary byte offsets, so they are not size
    truncated here (only forward extension is bounded); archive random-access is a
    tracked follow-up.
    """
    if index.seek_mode != "byte":
        return g_start_req, g_end_req

    total = index.total_groups

    def byte_at(group_index: int) -> int:
        if group_index >= total:
            return index.size
        return _byte_start(index.boundaries[group_index])

    if order == "tail":
        g_end = g_end_req
        k = g_end_req - g_start_req
        while k > 1:
            resume = index.boundaries[_resume_group(index, g_end - k)]
            if byte_at(g_end) - _byte_start(resume) <= max_span:
                break
            k -= 1
        return g_end - k, g_end

    g_start = g_start_req
    base = _byte_start(index.boundaries[_resume_group(index, g_start)])
    k = g_end_req - g_start_req
    while k > 1:
        if byte_at(g_start + k) - base <= max_span:
            break
        k -= 1
    return g_start, g_start + k


# --------------------------------------------------------------------------- #
# Raw-line resume iterators
# --------------------------------------------------------------------------- #


def _iter_file_from(path: str, start_byte: int, start_line_no: int, size: int) -> Iterator[RawLine]:
    """Stream positioned :class:`RawLine`s from ``start_byte`` to the snapshot size."""
    offset = start_byte
    line_no = start_line_no
    with open(path, "rb") as handle:
        handle.seek(start_byte)
        for raw_bytes in handle:
            if offset >= size:
                break
            text = raw_bytes.decode("utf-8", errors="replace")
            yield RawLine(byte_offset=offset, raw_line_no=line_no, text=text)
            offset += len(raw_bytes)
            line_no += 1


def _iter_lines_from(lines: list[str], start_line_no: int) -> Iterator[RawLine]:
    """Stream archive lines from ``start_line_no`` (line-seek mode)."""
    for i in range(start_line_no, len(lines)):
        yield RawLine(byte_offset=None, raw_line_no=i, text=lines[i])


def _track_budget(
    raws: Iterable[RawLine], box: list[int], *, start_byte: int, seek_mode: str
) -> Iterator[RawLine]:
    """Pass-through that records bytes consumed since the resume point into ``box``."""
    for raw in raws:
        if seek_mode == "byte" and raw.byte_offset is not None:
            box[0] = raw.byte_offset - start_byte
        else:
            box[0] += len(raw.text.encode("utf-8"))
        yield raw


# --------------------------------------------------------------------------- #
# Render helpers
# --------------------------------------------------------------------------- #


def _seed_stubs(state: RenderState, index: TranscriptIndex, window_start_parsed_index: int) -> None:
    """Seed throwaway pending entries for tools opened before the window start.

    A ``tool_result`` for one of these ids then hits the ``id in
    pending_tool_calls`` bypass and is absorbed into the stub (suppressed) instead
    of emitting an orphan group — exactly as a full render suppresses it (the
    renderer never evicts resolved pending calls).
    """
    for tool_id, first_idx in index.tool_first_open.items():
        if first_idx < window_start_parsed_index:
            state.pending_tool_calls[tool_id] = RenderedToolCall(
                id=tool_id,
                tool_name="",
                server_name="",
                tool_type="",
                arguments={},
            )


def _window_tool_calls(
    completed: list[RenderedMessage], resume_group: int, g_start: int, g_end: int
) -> list[RenderedToolCall]:
    """Unresolved tool calls opened inside ``[g_start, g_end)`` (forward-extend on)."""
    targets: list[RenderedToolCall] = []
    for k, group in enumerate(completed):
        group_index = resume_group + k
        if not (g_start <= group_index < g_end):
            continue
        for block in group.content_blocks:
            if block.type == "tool_chain" and block.tool_calls:
                targets.extend(tc for tc in block.tool_calls if tc.status != "completed")
    return targets


def _slice_window(
    completed: list[RenderedMessage], resume_group: int, g_start: int, g_end: int
) -> tuple[list[RenderedMessage], dict[int, RenderedMessage]]:
    """Select ``[g_start, g_end)`` from forward-rendered groups, dropping lookback."""
    groups: list[RenderedMessage] = []
    by_index: dict[int, RenderedMessage] = {}
    for k, group in enumerate(completed):
        group_index = resume_group + k
        if g_start <= group_index < g_end:
            groups.append(group)
            by_index[group_index] = group
    return groups, by_index


def _replay_adjustments(
    index: TranscriptIndex, by_index: dict[int, RenderedMessage], g_start: int, g_end: int
) -> None:
    """Replay index-resolved post-pass mutations onto in-window groups.

    Adjustments are keyed by rendered group (resolved at index build, which saw
    the true EOF), so a window that may not reach EOF still applies e.g. Droid
    sidecar usage to the correct group. The target message is the sole
    usage-bearing message of its group for every adjustment produced today, so a
    plain ``setattr`` reproduces the full render.
    """
    for adjustment in index.post_pass_adjustments:
        if g_start <= adjustment.group_index < g_end:
            target = by_index.get(adjustment.group_index)
            if target is not None:
                setattr(target, adjustment.field, adjustment.value)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def render_window(
    path: str,
    source: str,
    session_id: str | None,
    index: TranscriptIndex,
    *,
    limit: int,
    offset: int,
    order: str = "tail",
    lines: list[str] | None = None,
    max_span: int = MAX_WINDOW_SPAN_BYTES,
) -> WindowResult:
    """Render the rendered-group window ``[g_start, g_end)`` of a transcript.

    Args:
        path: Live JSONL transcript path (byte-seek) or archive path (line-seek).
        source: Resolved CLI source (selects the parser).
        session_id: Session ref (drives ``RenderedMessage.id``).
        index: Cached boundary index for this snapshot.
        limit: Requested page size (groups); clamped to ``>= 1``.
        offset: Groups to skip from the page's reference edge.
        order: ``"tail"`` (newest-first paging) or ``"head"`` (oldest-first).
        lines: Decompressed archive lines, required when ``index.seek_mode`` is
            ``"line"``.
        max_span: Byte ceiling for lookback + page + forward extension.

    Returns:
        A :class:`WindowResult` whose ``groups`` are oldest-first.
    """
    total = index.total_groups
    limit = max(1, limit)
    offset = max(0, offset)

    g_start_req, g_end_req = _requested_range(total, limit, offset, order)
    if g_start_req >= g_end_req:
        return WindowResult(
            groups=[],
            returned_count=0,
            total_groups=total,
            parsed_message_count=index.parsed_message_count,
        )

    g_start, g_end = _select_range(index, g_start_req, g_end_req, order, max_span)
    resume_group = _resume_group(index, g_start)
    resume_boundary = index.boundaries[resume_group]
    window_start_parsed_index = index.boundaries[g_start].parsed_index_start

    parser = _get_parser(source, session_id=session_id, transcript_path=path)
    state = RenderState()
    _seed_stubs(state, index, window_start_parsed_index)

    if index.seek_mode == "byte":
        start_byte = _byte_start(resume_boundary)
        raws: Iterable[RawLine] = _iter_file_from(
            path, start_byte, resume_boundary.raw_line_start, index.size
        )
    else:
        if lines is None:
            raise ValueError("line-seek index requires decompressed archive lines")
        start_byte = 0
        raws = _iter_lines_from(lines, resume_boundary.raw_line_start)

    box = [0]
    tracked = _track_budget(raws, box, start_byte=start_byte, seek_mode=index.seek_mode)

    completed: list[RenderedMessage] = []
    need_completed = g_end - resume_group
    extension_targets: list[RenderedToolCall] | None = None
    budget_exhausted = False
    reached_eof = False

    for event in parser.iter_parse_events(tracked, start_index=resume_boundary.parsed_index_start):
        for record in event.records:
            if not isinstance(record, ParsedMessage):
                continue
            done, state = render_incremental(
                [record], state, session_id=session_id, error_log=parser.error_log
            )
            if done:
                completed.extend(done)

        if extension_targets is None:
            # Page phase: the page is materialized once its successor group opens
            # (group g_end-1 has flushed). Windows ending at EOF flush after the
            # loop instead, so only bound non-EOF windows here.
            if g_end < total and len(completed) >= need_completed:
                extension_targets = _window_tool_calls(completed, resume_group, g_start, g_end)
                if not extension_targets:
                    break
        else:
            # Forward-extension phase: read until in-window tools resolve or the
            # span budget is hit (degraded).
            if all(tc.status == "completed" for tc in extension_targets):
                break
            if box[0] > max_span:
                budget_exhausted = True
                break
    else:
        reached_eof = True

    if reached_eof and state.current_message is not None:
        completed.append(state.current_message)

    groups, by_index = _slice_window(completed, resume_group, g_start, g_end)
    _replay_adjustments(index, by_index, g_start, g_end)

    degraded = bool(
        budget_exhausted
        and extension_targets
        and any(tc.status != "completed" for tc in extension_targets)
    )
    return WindowResult(
        groups=groups,
        returned_count=len(groups),
        total_groups=total,
        parsed_message_count=index.parsed_message_count,
        degraded=degraded,
        degraded_reason="max_span_exceeded" if degraded else None,
    )
