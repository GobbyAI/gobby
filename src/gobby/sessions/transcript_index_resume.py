"""Hydrate live transcript index appenders from matching sidecars."""

from __future__ import annotations

import io
from copy import copy
from typing import TYPE_CHECKING, cast

from gobby.sessions.transcript_index import TranscriptIndex, TranscriptIndexAppender
from gobby.sessions.transcript_index_sidecar import load_index_sidecar
from gobby.sessions.transcript_renderer import RenderedMessage, RenderState
from gobby.sessions.transcripts import get_parser

if TYPE_CHECKING:
    from gobby.sessions.transcripts.base import BaseTranscriptParser


def _next_parser_index(index: TranscriptIndex) -> int:
    return (
        index.next_parser_index
        if index.next_parser_index is not None
        else index.parsed_message_count
    )


def _next_raw_line_no(index: TranscriptIndex) -> int:
    return index.next_raw_line_no if index.next_raw_line_no is not None else index.raw_record_count


def _seed_resolved_tool_ids(state: RenderState, index: TranscriptIndex) -> None:
    """Use the existing suppression lookup without walking historical tool IDs."""
    state.pre_window_tool_first_open = index.tool_first_open
    state.pre_window_boundary_index = _next_parser_index(index)


def _seed_current_message_stub(state: RenderState, index: TranscriptIndex) -> str | None:
    if not index.boundaries:
        return None
    boundary = index.boundaries[-1]
    message_id = (
        f"resume-{boundary.role}-{boundary.timestamp.timestamp()}-{boundary.parsed_index_start}"
    )
    state.current_message = RenderedMessage(
        id=message_id,
        role=boundary.role,
        content="",
        timestamp=boundary.timestamp,
    )
    return message_id


def hydrate_appender_from_index(
    appender: TranscriptIndexAppender,
    index: TranscriptIndex,
) -> TranscriptIndexAppender:
    """Seed an incremental appender from a matching persisted index."""
    state = RenderState()
    _seed_resolved_tool_ids(state, index)
    current_id = _seed_current_message_stub(state, index)

    return appender.hydrate_from_index(
        index=index,
        state=state,
        current_id=current_id,
        next_parser_index=_next_parser_index(index),
        next_raw_line_no=_next_raw_line_no(index),
    )


def extend_index_from_file(
    path: str,
    source: str,
    session_id: str | None,
    *,
    mtime_ns: int,
    size: int,
    prior: TranscriptIndex | None = None,
) -> TranscriptIndex | None:
    """Extend a persisted byte index over appended lines instead of rebuilding it.

    Returns ``None`` when no sidecar covers an append-only prefix of the file, the
    parser cannot resume from persisted state, or the prefix ends mid-line.
    """
    parser = cast(
        "BaseTranscriptParser",
        get_parser(source, session_id=session_id, transcript_path=path),
    )
    if not parser.supports_incremental_state:
        return None
    prior = prior or load_index_sidecar(
        path,
        source,
        session_id,
        seek_mode="byte",
        mtime_ns=mtime_ns,
        size=size,
        allow_append=True,
    )
    if prior is None:
        return None
    with open(path, "rb") as handle:
        if prior.size:
            handle.seek(prior.size - 1)
            if handle.read(1) != b"\n":
                return None
        tail = handle.read(size - prior.size)

    lines: list[str] = []
    offsets: list[int] = []
    offset = prior.size
    for raw_bytes in io.BytesIO(tail):
        lines.append(raw_bytes.decode("utf-8", errors="replace"))
        offsets.append(offset)
        offset += len(raw_bytes)
    # Scalars and rebind-only fields retain the previous snapshot. Grow-only
    # containers belong to the resident cache; append rollback only removes the
    # speculative suffix, rather than copying all historical list/dict spines.
    from gobby.sessions.transcript_index_sidecar import (
        clone_persistence_state,
        refresh_source_snapshot,
    )

    working = copy(prior)
    clone_persistence_state(working)
    boundary_count = len(prior.boundaries)
    parsed_boundary_count = len(prior.parsed_boundaries)
    tool_count = len(prior.tool_first_open)
    appender = TranscriptIndexAppender(source, session_id, path, parser=parser)
    hydrate_appender_from_index(appender, working)
    try:
        appender.append_positioned_lines(lines, offsets, mtime_ns=mtime_ns, size=size)
        refresh_source_snapshot(path, working)
        return appender.snapshot(mtime_ns=mtime_ns, size=size)
    except BaseException:
        del prior.boundaries[boundary_count:]
        del prior.parsed_boundaries[parsed_boundary_count:]
        while len(prior.tool_first_open) > tool_count:
            prior.tool_first_open.popitem()
        raise
