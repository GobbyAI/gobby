from datetime import UTC, datetime

import pytest

from gobby.sessions.transcript_render_models import RenderState
from gobby.sessions.transcript_renderer import render_incremental, render_transcript
from gobby.sessions.transcripts.base import ParsedMessage, TranscriptParserErrorLog

pytestmark = pytest.mark.unit


def _message(
    *,
    index: int,
    role: str,
    content: str = "",
    content_type: str = "text",
    tool_name: str | None = None,
    tool_input: dict[str, object] | None = None,
    tool_result: dict[str, object] | None = None,
    tool_use_id: str | None = None,
) -> ParsedMessage:
    return ParsedMessage(
        index=index,
        role=role,
        content=content,
        content_type=content_type,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_result=tool_result,
        tool_use_id=tool_use_id,
        timestamp=datetime(2026, 6, 27, tzinfo=UTC),
        raw_json={"index": index, "type": content_type},
    )


def test_render_transcript_orphan_tool_result_becomes_completed_tool_chain(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    error_log = TranscriptParserErrorLog("orphan-tool-result")
    result = {"output": "done"}

    rendered = render_transcript(
        [
            _message(
                index=1,
                role="tool",
                content="done",
                content_type="tool_result",
                tool_name=None,
                tool_result=result,
            )
        ],
        session_id="session-1",
        error_log=error_log,
    )

    block = rendered[0].content_blocks[0]
    assert block.type == "tool_chain"
    assert block.block_type is None
    assert block.raw is None
    assert block.tool_calls is not None
    tool_call = block.tool_calls[0]
    assert tool_call.tool_name == "unknown_result"
    assert tool_call.status == "completed"
    assert tool_call.result is not None
    assert tool_call.result.content == result
    assert tool_call.result.kind == "json"
    assert not error_log.log_path.exists() or error_log.log_path.read_text() == ""


def test_render_incremental_windowed_orphan_reuses_tool_use_id() -> None:
    completed, state = render_incremental(
        [
            _message(
                index=2,
                role="tool",
                content="window result",
                content_type="tool_result",
                tool_name="Read",
                tool_result={"content": "window result"},
                tool_use_id="tool-1",
            )
        ],
        RenderState(),
        session_id="session-1",
    )

    assert completed == []
    assert state.current_message is not None
    block = state.current_message.content_blocks[0]
    assert block.type == "tool_chain"
    assert block.tool_calls is not None
    tool_call = block.tool_calls[0]
    assert tool_call.id == "tool-1"
    assert tool_call.tool_name == "Read"
    assert tool_call.status == "completed"
    assert tool_call.result is not None
    assert tool_call.result.content == {"content": "window result"}


def test_paired_tool_result_still_attaches_to_real_tool_chain() -> None:
    rendered = render_transcript(
        [
            _message(
                index=1,
                role="assistant",
                content_type="tool_use",
                tool_name="Read",
                tool_input={"file_path": "README.md"},
                tool_use_id="tool-1",
            ),
            _message(
                index=2,
                role="tool",
                content="file contents",
                content_type="tool_result",
                tool_result={"content": "file contents"},
                tool_use_id="tool-1",
            ),
        ],
        session_id="session-1",
    )

    assert len(rendered) == 1
    assert len(rendered[0].content_blocks) == 1
    block = rendered[0].content_blocks[0]
    assert block.type == "tool_chain"
    assert block.tool_calls is not None
    tool_call = block.tool_calls[0]
    assert tool_call.id == "tool-1"
    assert tool_call.status == "completed"
    assert tool_call.result is not None
    assert tool_call.result.content == {"content": "file contents"}
