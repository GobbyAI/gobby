"""Hydrate live transcript index appenders from matching sidecars."""

from __future__ import annotations

from gobby.sessions.transcript_index import TranscriptIndex, TranscriptIndexAppender
from gobby.sessions.transcript_renderer import RenderedMessage, RenderedToolCall, RenderState


def _next_parser_index(index: TranscriptIndex) -> int:
    return (
        index.next_parser_index
        if index.next_parser_index is not None
        else index.parsed_message_count
    )


def _next_raw_line_no(index: TranscriptIndex) -> int:
    return index.next_raw_line_no if index.next_raw_line_no is not None else index.raw_record_count


def _seed_pending_tool_stubs(state: RenderState, index: TranscriptIndex) -> None:
    parser_index = _next_parser_index(index)
    for tool_id, first_index in index.tool_first_open.items():
        if first_index >= parser_index:
            continue
        state.pending_tool_calls[tool_id] = RenderedToolCall(
            id=tool_id,
            tool_name="",
            server_name="",
            tool_type="",
            arguments={},
        )


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
    _seed_pending_tool_stubs(state, index)
    current_id = _seed_current_message_stub(state, index)

    appender.index = index
    appender._state = state
    appender._role_counts = dict(index.role_message_counts)
    appender._prev_current_id = current_id
    appender._next_start_index = _next_parser_index(index)
    appender._next_raw_line_no = _next_raw_line_no(index)
    appender._safe_to_start_event = (
        index.safe_to_start_event if index.safe_to_start_event is not None else True
    )
    return appender
