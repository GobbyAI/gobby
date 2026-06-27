from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.sessions.observation_tracker import ObservationTracker
from gobby.sessions.transcript_renderer import RenderState, render_incremental, render_transcript
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.unmodeled_observations import UnmodeledObservationStore

pytestmark = pytest.mark.unit


def _message(
    *,
    index: int,
    content_type: str,
    raw_type: str,
    tool_name: str | None = None,
    tool_input: dict[str, object] | None = None,
) -> ParsedMessage:
    raw_json = {"type": raw_type, "payload": {"tool": tool_name, "input": tool_input or {}}}
    return ParsedMessage(
        index=index,
        role="assistant",
        content="",
        content_type=content_type,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_result=None,
        timestamp=datetime(2026, 6, 27, tzinfo=UTC),
        raw_json=raw_json,
        tool_use_id=f"tool-{index}" if tool_name else None,
        source="codex",
        source_ref=str(index),
        source_line=index,
    )


def test_unknown_block_type_records_once_across_rerenders(temp_db: HubDatabase) -> None:
    store = UnmodeledObservationStore(temp_db)
    msg = _message(index=101, content_type="new_provider_block", raw_type="new_provider_block")

    rendered = render_transcript(
        [msg],
        session_id="session-render-block",
        source="codex",
        observation_tracker=ObservationTracker(store),
    )
    _, _state = render_incremental(
        [msg],
        RenderState(),
        session_id="session-render-block",
        source="codex",
        observation_tracker=ObservationTracker(store),
    )

    rows = [
        row
        for row in store.list_observations(source="codex", kind="block_type")
        if row.name == "new_provider_block"
    ]
    events = temp_db.fetchall(
        "SELECT id FROM unmodeled_observation_events WHERE session_id = %s AND name = %s",
        ("session-render-block", "new_provider_block"),
    )
    assert rendered[0].content_blocks[0].type == "unknown"
    assert len(events) == 1
    assert rows[0].count == 1


def test_unknown_tool_name_records_classification(temp_db: HubDatabase) -> None:
    store = UnmodeledObservationStore(temp_db)
    msg = _message(
        index=102,
        content_type="tool_use",
        raw_type="function_call",
        tool_name="MysteryTool",
        tool_input={"arg": "value"},
    )

    render_transcript(
        [msg],
        session_id="session-render-tool",
        source="codex",
        observation_tracker=ObservationTracker(store),
    )

    rows = [
        row
        for row in store.list_observations(source="codex", kind="tool_name")
        if row.name == "MysteryTool"
    ]
    assert len(rows) == 1
    assert rows[0].count == 1
    assert rows[0].server_name == "unknown"
    assert rows[0].tool_type == "unknown"


def test_synthetic_unknown_tool_name_is_excluded(temp_db: HubDatabase) -> None:
    store = UnmodeledObservationStore(temp_db)
    msg = _message(
        index=103,
        content_type="tool_use",
        raw_type="synthetic_tool",
        tool_name="unknown",
    )

    render_transcript(
        [msg],
        session_id="session-render-synthetic",
        source="codex",
        observation_tracker=ObservationTracker(store),
    )

    rows = [
        row
        for row in store.list_observations(source="codex", kind="tool_name")
        if row.name == "unknown"
    ]
    assert rows == []
