"""Tests for HookTranscriptAssembler."""

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.sessions.transcripts.hook_assembler import HookTranscriptAssembler

pytestmark = pytest.mark.unit


@pytest.fixture
def assembler() -> HookTranscriptAssembler:
    return HookTranscriptAssembler()


def _make_event(
    event_type: HookEventType,
    data: dict,
    source: SessionSource = SessionSource.WINDSURF,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="ext-123",
        source=source,
        timestamp=datetime(2026, 2, 7, 12, 0, 0, tzinfo=UTC),
        data=data,
    )


class TestBeforeAgent:
    def test_emits_user_message(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(HookEventType.BEFORE_AGENT, {"user_input": "Hello world"})
        msgs = assembler.process_event("sess-1", event)

        assert len(msgs) == 1
        assert msgs[0].role == "user"
        assert msgs[0].content == "Hello world"
        assert msgs[0].content_type == "text"
        assert msgs[0].index == 0

    def test_prompt_field_fallback(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(HookEventType.BEFORE_AGENT, {"prompt": "Fix the bug"})
        msgs = assembler.process_event("sess-1", event)

        assert len(msgs) == 1
        assert msgs[0].content == "Fix the bug"

    def test_empty_content_skips(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(HookEventType.BEFORE_AGENT, {})
        msgs = assembler.process_event("sess-1", event)

        assert msgs == []


class TestAfterAgent:
    def test_emits_assistant_message(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(HookEventType.AFTER_AGENT, {"response": "Here is the fix"})
        msgs = assembler.process_event("sess-1", event)

        assert len(msgs) == 1
        assert msgs[0].role == "assistant"
        assert msgs[0].content == "Here is the fix"
        assert msgs[0].content_type == "text"

    def test_no_response_skips(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(HookEventType.AFTER_AGENT, {"status": "done"})
        msgs = assembler.process_event("sess-1", event)

        assert msgs == []


class TestBeforeTool:
    def test_emits_tool_use(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            {"tool_name": "read_file", "tool_input": {"path": "/tmp/foo.py"}},
        )
        msgs = assembler.process_event("sess-1", event)

        assert len(msgs) == 1
        assert msgs[0].role == "assistant"
        assert msgs[0].content_type == "tool_use"
        assert msgs[0].tool_name == "read_file"
        assert msgs[0].tool_input == {"path": "/tmp/foo.py"}

    def test_copilot_camel_case_fields(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            {"toolName": "editFile", "toolArgs": {"file": "main.py"}},
            source=SessionSource.COPILOT,
        )
        msgs = assembler.process_event("sess-1", event)

        assert len(msgs) == 1
        assert msgs[0].tool_name == "editFile"
        assert msgs[0].tool_input == {"file": "main.py"}


class TestAfterTool:
    def test_emits_tool_result(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(
            HookEventType.AFTER_TOOL,
            {"tool_name": "read_file", "tool_output": {"text": "file contents"}},
        )
        msgs = assembler.process_event("sess-1", event)

        assert len(msgs) == 1
        assert msgs[0].role == "tool"
        assert msgs[0].content_type == "tool_result"
        assert msgs[0].tool_name == "read_file"
        assert msgs[0].tool_result == {"text": "file contents"}
        assert msgs[0].content == "file contents"

    def test_copilot_tool_result_nested(self, assembler: HookTranscriptAssembler) -> None:
        """Copilot nests result under toolResult.textResultForLlm."""
        event = _make_event(
            HookEventType.AFTER_TOOL,
            {
                "toolName": "readFile",
                "toolResult": {"textResultForLlm": "file contents here"},
            },
            source=SessionSource.COPILOT,
        )
        msgs = assembler.process_event("sess-1", event)

        assert len(msgs) == 1
        assert msgs[0].tool_result == {"text": "file contents here"}
        assert msgs[0].content == "file contents here"

    def test_string_output_wrapped(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(
            HookEventType.AFTER_TOOL,
            {"tool_name": "bash", "tool_output": "exit code 0"},
        )
        msgs = assembler.process_event("sess-1", event)

        assert msgs[0].tool_result == {"text": "exit code 0"}


class TestIndexTracking:
    def test_index_increments_per_session(self, assembler: HookTranscriptAssembler) -> None:
        events = [
            _make_event(HookEventType.BEFORE_AGENT, {"user_input": "q1"}),
            _make_event(HookEventType.BEFORE_TOOL, {"tool_name": "t1"}),
            _make_event(HookEventType.AFTER_TOOL, {"tool_name": "t1", "tool_output": "r1"}),
            _make_event(HookEventType.AFTER_AGENT, {"response": "a1"}),
        ]
        indices = []
        for ev in events:
            msgs = assembler.process_event("sess-1", ev)
            for m in msgs:
                indices.append(m.index)

        assert indices == [0, 1, 2, 3]

    def test_separate_sessions_independent(self, assembler: HookTranscriptAssembler) -> None:
        ev1 = _make_event(HookEventType.BEFORE_AGENT, {"user_input": "q1"})
        ev2 = _make_event(HookEventType.BEFORE_AGENT, {"user_input": "q2"})

        msgs_a = assembler.process_event("sess-a", ev1)
        msgs_b = assembler.process_event("sess-b", ev2)
        msgs_a2 = assembler.process_event("sess-a", ev1)

        assert msgs_a[0].index == 0
        assert msgs_b[0].index == 0
        assert msgs_a2[0].index == 1  # second message in sess-a


class TestWindsurfToolInfo:
    def test_windsurf_post_write_code(self, assembler: HookTranscriptAssembler) -> None:
        """Windsurf post_write_code provides tool_name and tool_output."""
        event = _make_event(
            HookEventType.AFTER_TOOL,
            {
                "tool_name": "write_code",
                "tool_input": {"file": "app.py", "content": "print('hi')"},
                "tool_output": {"text": "File written successfully"},
            },
            source=SessionSource.WINDSURF,
        )
        msgs = assembler.process_event("sess-1", event)

        assert len(msgs) == 1
        assert msgs[0].tool_name == "write_code"
        assert msgs[0].content == "File written successfully"


class TestUnhandledEvents:
    def test_session_start_ignored(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(HookEventType.SESSION_START, {"cwd": "/tmp"})
        msgs = assembler.process_event("sess-1", event)
        assert msgs == []

    def test_session_end_ignored(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(HookEventType.SESSION_END, {})
        msgs = assembler.process_event("sess-1", event)
        assert msgs == []

    def test_pre_compact_ignored(self, assembler: HookTranscriptAssembler) -> None:
        event = _make_event(HookEventType.PRE_COMPACT, {})
        msgs = assembler.process_event("sess-1", event)
        assert msgs == []
