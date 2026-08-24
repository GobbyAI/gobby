"""Hydrate live transcript index appenders from matching sidecars."""

from __future__ import annotations

from gobby.sessions.transcript_index import TranscriptIndex, TranscriptIndexAppender
from gobby.sessions.transcript_renderer import RenderedMessage, RenderState


def _next_parser_index(index: TranscriptIndex) -> int:
    return (
        index.next_parser_index
        if index.next_parser_index is not None
        else index.parsed_message_count
    )


def _next_raw_line_no(index: TranscriptIndex) -> int:
    return index.next_raw_line_no if index.next_raw_line_no is not None else index.raw_record_count


def _seed_resolved_tool_ids(state: RenderState, index: TranscriptIndex) -> None:
    """Mark every pre-resume tool call resolved, so a result for one is absorbed.

    A ``tool_result`` for one of these ids then takes the ``knows_tool_call``
    bypass and is suppressed instead of rendered as an orphan group -- the same
    absorption the payload-less pending stubs seeded here used to provide. Stubs
    put that absorption in ``pending_tool_calls``, where a call whose result had
    already arrived pre-resume could never be popped, and every per-batch
    ``TranscriptIndexAppender.clone`` deep-copied the whole permanently-pending
    population (#20875). The resolved-id set is shared by
    ``RenderState.__deepcopy__`` rather than copied, so remembering every
    pre-resume call costs the clone nothing. A call still genuinely in flight
    across the restart loses nothing either: its stub carried no name,
    arguments, or owner message, so pairing with it already just consumed the
    result.
    """
    parser_index = _next_parser_index(index)
    for tool_id, first_index in index.tool_first_open.items():
        if first_index >= parser_index:
            continue
        state.resolved_tool_call_ids.add(tool_id)


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
