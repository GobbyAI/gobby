import json
from datetime import UTC, datetime

import pytest

from gobby.sessions.transcript_renderer import render_transcript
from gobby.sessions.transcripts.base import (
    BaseTranscriptParser,
    ParsedMessage,
    TokenUsage,
    TranscriptParserErrorLog,
    _classify_decode_failure,
    _unknown_block_message,
)

pytestmark = pytest.mark.unit


def test_parser_error_log_creation(tmp_path, monkeypatch):
    # Mock home directory for testing
    monkeypatch.setenv("HOME", str(tmp_path))

    cli_name = "test-cli"
    error_log = TranscriptParserErrorLog(cli_name)

    expected_path = tmp_path / ".gobby" / "logs" / f"{cli_name}-parser-error.log"
    assert error_log.log_path == expected_path

    # Trigger logging
    error_log.log_malformed_line(1, "session-1", '{"bad": "json"', "Unexpected EOF")

    assert expected_path.exists()
    content = expected_path.read_text()
    assert "line:1" in content
    assert "session:session-1" in content
    assert "Malformed line: Unexpected EOF" in content
    assert '{"bad": "json"' in content


def test_log_unknown_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cli_name = "test-cli-unknown"
    error_log = TranscriptParserErrorLog(cli_name)

    raw = {"type": "weird", "data": "value"}
    error_log.log_unknown_block(10, "session-2", "weird", raw)

    content = error_log.log_path.read_text()
    assert "line:10" in content
    assert "session:session-2" in content
    assert "Unknown block type: weird" in content
    assert json.dumps(raw) in content


def test_renderer_logs_unknown_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cli_name = "test-cli-renderer"
    error_log = TranscriptParserErrorLog(cli_name)

    # Mock a ParsedMessage with an unknown content_type
    msg = ParsedMessage(
        index=5,
        role="assistant",
        content="some content",
        content_type="magic_block",
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime.now(UTC),
        raw_json={"type": "magic_block", "extra": "data"},
    )

    render_transcript([msg], session_id="session-3", error_log=error_log)

    content = error_log.log_path.read_text()
    assert "line:5" in content
    assert "session:session-3" in content
    assert "Unknown block type: magic_block" in content
    assert '"extra": "data"' in content


def test_unknown_block_message_preserves_raw_for_render_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    timestamp = datetime(2026, 6, 27, tzinfo=UTC)
    usage = TokenUsage(input_tokens=12, output_tokens=3)
    raw = {"type": "magic_block", "text": "visible text", "extra": {"z": 1}}

    msg = _unknown_block_message(
        index=7,
        block_type="magic_block",
        raw=raw,
        timestamp=timestamp,
        message_id="msg-1",
        model="test-model",
        usage=usage,
    )

    assert msg.role == "assistant"
    assert msg.content == "visible text"
    assert msg.content_type == "magic_block"
    assert msg.raw_json == raw
    assert msg.timestamp == timestamp
    assert msg.message_id == "msg-1"
    assert msg.model == "test-model"
    assert msg.usage is usage

    rendered = render_transcript([msg], session_id="session-unknown", cli_name="test-cli")

    assert len(rendered) == 1
    block = rendered[0].content_blocks[0]
    assert block.type == "unknown"
    assert block.content == "visible text"
    assert block.block_type == "magic_block"
    assert block.raw == raw


def test_unknown_block_message_uses_content_or_fallback():
    timestamp = datetime(2026, 6, 27, tzinfo=UTC)

    content_msg = _unknown_block_message(
        index=1,
        block_type="content_block",
        raw={"type": "content_block", "content": "from content"},
        timestamp=timestamp,
    )
    fallback_msg = _unknown_block_message(
        index=2,
        block_type="opaque_block",
        raw={"type": "opaque_block", "meta": True},
        timestamp=timestamp,
    )

    assert content_msg.content == "from content"
    assert fallback_msg.content == "[unsupported block: opaque_block]"


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        ("", "empty"),
        ("  \n", "empty"),
        ("status: ok", "non_json"),
        ("true", "non_json"),
        ("[1, 2]", "non_json"),
        ('"done"', "non_json"),
        ('{"name":', "truncated"),
        ('{"name": "value"', "truncated"),
        ('["item"', "truncated"),
        ('"unfinished', "truncated"),
        ('{"name" "value"}', "non_json"),
    ],
)
def test_classify_decode_failure(raw_text, expected):
    try:
        json.loads(raw_text)
    except json.JSONDecodeError as exc:
        error = exc
    else:
        error = json.JSONDecodeError("synthetic non-object JSON", raw_text, 0)

    assert _classify_decode_failure(raw_text, error) == expected


def test_parser_logs_malformed_line(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    class MockParser(BaseTranscriptParser):
        def parse_line(self, line, index):
            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                self.error_log.log_malformed_line(index, self.session_id, line, str(e))
                return None

    parser = MockParser("mock-cli-malformed", session_id="session-4")
    parser.parse_line('{"valid": "json"}', 1)
    parser.parse_line("invalid json", 2)

    content = parser.error_log.log_path.read_text()
    assert "line:2" in content
    assert "session:session-4" in content
    assert "Malformed line" in content
    assert "invalid json" in content


def test_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cli_name = "test-cli-rotation"
    error_log = TranscriptParserErrorLog(cli_name)

    # We want to test rotation at 10MB, but creating 10MB of logs in a test is slow.
    # We can check if the handler is RotatingFileHandler with correct maxBytes.
    from logging.handlers import RotatingFileHandler

    handler = error_log.logger.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.baseFilename == str(error_log.log_path)
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 5
