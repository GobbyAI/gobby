from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.sessions.observation_tracker import ObservationTracker
from gobby.sessions.transcript_renderer import RenderState, render_incremental, render_transcript
from gobby.sessions.transcripts.base import UNMODELED_RECORD_CONTENT_TYPE, ParsedMessage
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.unmodeled_observations import UnmodeledObservationStore

pytestmark = pytest.mark.unit

# unmodeled_observation_events.session_id is a native uuid column; non-uuid
# session ids are stored as NULL by the tracker guard.
SESSION_RENDER_BLOCK = "aeaeaeae-0000-4000-8000-00000000ab01"
SESSION_RENDER_TOOL = "aeaeaeae-0000-4000-8000-00000000ab02"
SESSION_RENDER_SYNTHETIC = "aeaeaeae-0000-4000-8000-00000000ab03"
SESSION_CLAUDE_UNKNOWN = "aeaeaeae-0000-4000-8000-00000000ab04"


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
        session_id=SESSION_RENDER_BLOCK,
        source="codex",
        observation_tracker=ObservationTracker(store),
    )
    _, _state = render_incremental(
        [msg],
        RenderState(),
        session_id=SESSION_RENDER_BLOCK,
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
        (SESSION_RENDER_BLOCK, "new_provider_block"),
    )
    assert rendered[0].content_blocks[0].type == "unknown"
    assert len(events) == 1
    assert rows[0].count == 1


def test_non_uuid_session_id_stores_null_and_still_dedupes(temp_db: HubDatabase) -> None:
    """Non-uuid session ids become NULL in the uuid columns; the
    NULLS NOT DISTINCT dedup key still collapses repeat occurrences."""
    store = UnmodeledObservationStore(temp_db)
    msg = _message(index=104, content_type="odd_block", raw_type="odd_block")

    for _ in range(2):
        render_transcript(
            [msg],
            session_id="legacy-non-uuid-session",
            source="codex",
            observation_tracker=ObservationTracker(store),
        )

    events = temp_db.fetchall(
        "SELECT session_id FROM unmodeled_observation_events WHERE name = %s",
        ("odd_block",),
    )
    assert len(events) == 1
    assert events[0]["session_id"] is None

    rows = [
        row
        for row in store.list_observations(source="codex", kind="block_type")
        if row.name == "odd_block"
    ]
    assert len(rows) == 1
    assert rows[0].example_session_id is None


def test_unknown_tool_name_records_classification(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    store = UnmodeledObservationStore(temp_db)
    msg = _message(
        index=102,
        content_type="tool_use",
        raw_type="function_call",
        tool_name="MysteryTool",
        tool_input={"arg": "value"},
    )

    with caplog.at_level(logging.DEBUG, logger="gobby.sessions.unmodeled_observations"):
        render_transcript(
            [msg],
            session_id=SESSION_RENDER_TOOL,
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
    records = [r for r in caplog.records if getattr(r, "observed_name", None) == "MysteryTool"]
    assert len(records) == 1
    assert records[0].getMessage() == "Unmodeled transcript block observed"


def test_known_transcript_tools_do_not_record_unmodeled_observations(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    store = UnmodeledObservationStore(temp_db)
    messages = [
        _message(
            index=105,
            content_type="tool_use",
            raw_type="function_call",
            tool_name="call_tool",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
        ),
        _message(
            index=106,
            content_type="tool_use",
            raw_type="function_call",
            tool_name="mcp__gobby__call_tool",
            tool_input={"server_name": "gobby-memory", "tool_name": "create_memory"},
        ),
        _message(
            index=107,
            content_type="tool_use",
            raw_type="function_call",
            tool_name="mcp_gobby_call_tool",
            tool_input={"server_name": "gobby-skills", "tool_name": "get_skill"},
        ),
        _message(
            index=108,
            content_type="tool_use",
            raw_type="function_call",
            tool_name="update_plan",
            tool_input={"plan": [{"step": "Verify", "status": "in_progress"}]},
        ),
    ]

    with caplog.at_level(logging.DEBUG, logger="gobby.sessions.unmodeled_observations"):
        render_transcript(
            messages,
            session_id=SESSION_RENDER_TOOL,
            source="codex",
            observation_tracker=ObservationTracker(store),
        )

    names = {msg.tool_name for msg in messages}
    rows = [
        row
        for row in store.list_observations(source="codex", kind="tool_name")
        if row.name in names
    ]
    assert rows == []
    assert [
        r
        for r in caplog.records
        if getattr(r, "observed_name", None) in names
        and r.getMessage() == "Unmodeled transcript block observed"
    ] == []


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
        session_id=SESSION_RENDER_SYNTHETIC,
        source="codex",
        observation_tracker=ObservationTracker(store),
    )

    rows = [
        row
        for row in store.list_observations(source="codex", kind="tool_name")
        if row.name == "unknown"
    ]
    assert rows == []


def test_observe_block_type_does_not_raise_keyerror_on_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``observed_name`` extra key must not conflict with LogRecord.name.

    Regression: ``"name"`` was previously used in the ``extra`` dict, which
    Python's logging module reserves for the logger name. This raised
    ``KeyError: "Attempt to overwrite 'name' in LogRecord"`` on every grok
    session processor poll, crashing transcript ingestion.
    """
    import logging

    tracker = ObservationTracker(store=None)
    msg = _message(index=200, content_type="new_block", raw_type="new_block")

    # Force DEBUG so the discovery path in _observe fires (must not raise).
    with caplog.at_level(logging.DEBUG, logger="gobby.sessions.unmodeled_observations"):
        tracker.observe_block_type(
            msg,
            session_id="session-log-test",
            source="grok",
            block_type="new_block",
        )

    records = [r for r in caplog.records if getattr(r, "observed_name", None) == "new_block"]
    assert len(records) == 1
    assert records[0].getMessage() == "Unmodeled transcript block observed"
    # The conflict-free key carries the block name; LogRecord.name stays the logger.
    assert records[0].name == "gobby.sessions.unmodeled_observations"


def test_observe_tool_name_does_not_raise_keyerror_on_persist_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The debug-path extra dict must also use ``observed_name``.

    Covers the ``logger.debug`` call in the ``except Exception`` block when
    ``store.record()`` raises — the second ``extra`` dict that had ``"name"``.
    """
    import logging
    from unittest.mock import MagicMock

    failing_store = MagicMock(spec=UnmodeledObservationStore)
    failing_store.record.side_effect = RuntimeError("DB is down")

    tracker = ObservationTracker(store=failing_store)
    msg = _message(
        index=201,
        content_type="tool_use",
        raw_type="function_call",
        tool_name="FailingTool",
        tool_input={"x": 1},
    )

    # Force DEBUG so the except-block logger.debug fires (must not raise KeyError).
    with caplog.at_level(logging.DEBUG, logger="gobby.sessions.unmodeled_observations"):
        tracker.observe_tool_name(
            msg,
            session_id="session-log-test-debug",
            source="grok",
            tool_name="FailingTool",
            server_name="srv",
            tool_type="call",
        )

    failing_store.record.assert_called_once()
    # The except-block debug log fired (with the conflict-free key) without raising.
    debug_records = [
        r
        for r in caplog.records
        if r.getMessage() == "Failed to persist unmodeled transcript observation"
    ]
    assert len(debug_records) == 1
    assert getattr(debug_records[0], "observed_name", None) == "FailingTool"
    assert debug_records[0].name == "gobby.sessions.unmodeled_observations"


def test_claude_unknown_record_routes_to_t2_via_parse_lines(temp_db: HubDatabase) -> None:
    """End-to-end: a genuinely-unknown Claude record-level type, parsed through
    ClaudeTranscriptParser.parse_lines() (so annotate_record_source populates
    raw-line provenance), renders no card and records exactly one T2 block_type
    observation whose source/source_line/source_ref reflect the RAW line, not the
    parser index."""
    parser = ClaudeTranscriptParser(session_id="probe")
    lines = [
        # raw line 0 -> two parsed messages (text + tool_use): parser indices 0, 1
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "tool_use", "name": "Read", "id": "t1", "input": {}},
                    ]
                },
                "timestamp": "2024-01-01T12:00:00Z",
            }
        ),
        # raw line 1 -> the unknown record: parser index 2, raw_line_no 1
        json.dumps({"type": "brand-new-envelope", "x": 1, "timestamp": "2024-01-01T12:00:01Z"}),
    ]
    parsed = [m for m in parser.parse_lines(lines) if isinstance(m, ParsedMessage)]

    sentinel = next(m for m in parsed if m.content_type == UNMODELED_RECORD_CONTENT_TYPE)
    assert sentinel.content == "brand-new-envelope"
    assert sentinel.role == "system"
    # Provenance is the raw line (1), distinct from the parser index (2).
    assert sentinel.index == 2
    assert sentinel.source == "claude"
    assert sentinel.source_line == 1
    assert sentinel.source_ref == "1"

    fallback_source = "codex"
    assert sentinel.source != fallback_source
    store = UnmodeledObservationStore(temp_db)
    rendered = render_transcript(
        parsed,
        session_id=SESSION_CLAUDE_UNKNOWN,
        source=fallback_source,
        observation_tracker=ObservationTracker(store),
    )

    # No card: the sentinel produces no group and no "unknown" block.
    assert not [
        block for group in rendered for block in group.content_blocks if block.type == "unknown"
    ]

    rows = [
        row
        for row in store.list_observations(source="claude", kind="block_type")
        if row.name == "brand-new-envelope"
    ]
    assert len(rows) == 1
    assert rows[0].count == 1

    events = temp_db.fetchall(
        "SELECT source, source_ref, source_line FROM unmodeled_observation_events "
        "WHERE session_id = %s AND kind = 'block_type' AND name = %s",
        (SESSION_CLAUDE_UNKNOWN, "brand-new-envelope"),
    )
    assert len(events) == 1
    assert events[0]["source"] == "claude"
    assert events[0]["source_ref"] == "1"
    assert events[0]["source_line"] == 1
