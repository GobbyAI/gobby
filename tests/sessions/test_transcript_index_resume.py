"""Resume hydration must not recreate the per-batch clone tax (#20875).

Hydrating an appender used to seed a payload-less pending stub for every
pre-resume tool call. A call whose result had already arrived before the
restart could never pop its stub, so every subsequent per-batch appender clone
deep-copied the whole permanently-pending population. The ids now go into the
resolved-id record, which ``RenderState.__deepcopy__`` shares rather than
copies, while a late result for a pre-resume call is still absorbed instead of
rendered as an orphan.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.sessions.message_stats import empty_message_stats
from gobby.sessions.transcript_index import (
    GroupBoundary,
    TranscriptIndex,
    TranscriptIndexAppender,
)
from gobby.sessions.transcript_index_resume import hydrate_appender_from_index
from gobby.sessions.transcript_renderer import render_incremental
from gobby.sessions.transcripts.base import ParsedMessage

pytestmark = pytest.mark.unit

TOOL_CALLS = 500


def _index_with_prior_tool_calls(tool_calls: int = TOOL_CALLS) -> TranscriptIndex:
    timestamp = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    return TranscriptIndex(
        boundaries=[
            GroupBoundary(
                group_index=0,
                raw_line_start=0,
                byte_start=0,
                parsed_index_start=0,
                resume_safe=True,
                role="assistant",
                timestamp=timestamp,
            )
        ],
        total_groups=1,
        parsed_message_count=tool_calls * 2,
        raw_record_count=tool_calls * 2,
        source="claude",
        session_id="s1",
        seek_mode="byte",
        mtime_ns=1,
        size=1000,
        tool_first_open={f"toolu_{i}": i for i in range(tool_calls)},
        role_message_counts={"assistant": tool_calls, "user": tool_calls},
        session_stats=empty_message_stats(),
        next_parser_index=tool_calls * 2,
        next_raw_line_no=tool_calls * 2,
        safe_to_start_event=True,
    )


def test_resume_leaves_no_permanently_pending_stubs() -> None:
    appender = TranscriptIndexAppender("claude", "s1", None)
    hydrate_appender_from_index(appender, _index_with_prior_tool_calls())

    state = appender._state
    assert state.pending_tool_calls == {}
    assert len(state.resolved_tool_call_ids) == TOOL_CALLS
    # The record the ids moved into is the one deepcopy shares, so the
    # per-batch clone pays nothing for remembering them.
    cloned = appender.clone()
    assert cloned._state.pending_tool_calls == {}
    assert cloned._state.resolved_tool_call_ids is state.resolved_tool_call_ids


def test_a_tool_call_at_the_resume_point_is_not_marked_resolved() -> None:
    """Only calls strictly before the parser resume position are pre-resume."""
    index = _index_with_prior_tool_calls(tool_calls=3)
    index.tool_first_open["toolu_next"] = index.next_parser_index or 0
    appender = TranscriptIndexAppender("claude", "s1", None)

    hydrate_appender_from_index(appender, index)

    assert "toolu_next" not in appender._state.resolved_tool_call_ids


def test_a_late_result_for_a_pre_resume_call_is_absorbed_without_an_orphan() -> None:
    """A result for a call opened before the restart is consumed, not orphaned.

    This is the absorption the pending stubs used to provide; it now comes
    from the resolved-id record through the same ``knows_tool_call`` bypass.
    """
    appender = TranscriptIndexAppender("claude", "s1", None)
    hydrate_appender_from_index(appender, _index_with_prior_tool_calls())
    state = appender._state
    assert state.current_message is not None
    blocks_before = len(state.current_message.content_blocks)
    current_id_before = state.current_message.id

    late_result = ParsedMessage(
        index=TOOL_CALLS * 2,
        role="user",
        content="",
        content_type="tool_result",
        tool_name=None,
        tool_input=None,
        tool_result={"exit_code": 0, "stdout": "late", "stderr": ""},
        timestamp=datetime(2024, 1, 1, 12, 30, 0, tzinfo=UTC),
        raw_json={},
        tool_use_id="toolu_3",
    )
    completed, state = render_incremental([late_result], state, session_id="s1")

    assert completed == []
    assert state.current_message is not None
    assert state.current_message.id == current_id_before
    assert len(state.current_message.content_blocks) == blocks_before
