import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.config.logging import (
    DAEMON_LOG_FILENAME,
    ERRORS_LOG_FILENAME,
    HOOKS_LOG_FILENAME,
    MCP_LOG_FILENAME,
    LoggingSettings,
    resolved_log_path,
)
from gobby.sessions.transcript_renderer import render_transcript
from gobby.sessions.transcripts.base import (
    BaseTranscriptParser,
    ParsedMessage,
    TokenUsage,
    TranscriptParserErrorLog,
    _classify_decode_failure,
    _unknown_block_message,
)
from gobby.telemetry.logging import setup_file_logging

pytestmark = pytest.mark.unit


def test_parser_error_log_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs_dir = tmp_path / "custom-logs"
    monkeypatch.setenv("GOBBY_LOGGING_DIR", str(logs_dir))

    cli_name = "test-cli"
    error_log = TranscriptParserErrorLog(cli_name)

    expected_path = logs_dir / f"{cli_name}-parser-error.log"
    assert error_log.log_path == expected_path
    assert error_log.logger.propagate

    # A truncated partial write is kept at INFO and reaches the log file.
    raw = '{"bad": "json"'
    error: json.JSONDecodeError | None = None
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        error = exc
    assert error is not None
    error_log.log_decode_failure(1, "session-1", raw, error)

    assert expected_path.exists()
    content = expected_path.read_text()
    assert "line:1" in content
    assert "session:session-1" in content
    assert "Malformed line (truncated)" in content
    assert raw in content


def test_parser_warning_has_dedicated_primary_and_error_aggregate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = LoggingSettings(dir=str(tmp_path / "logs"), level="debug")
    monkeypatch.setenv("GOBBY_LOGGING_DIR", config.dir)
    setup_file_logging(config)
    error_log = TranscriptParserErrorLog("aggregate")

    error_log.logger.warning("parser-warning")

    assert "parser-warning" in error_log.log_path.read_text()
    assert "parser-warning" in resolved_log_path(config, ERRORS_LOG_FILENAME).read_text()
    for filename in (DAEMON_LOG_FILENAME, HOOKS_LOG_FILENAME, MCP_LOG_FILENAME):
        assert "parser-warning" not in resolved_log_path(config, filename).read_text()


def test_parser_logger_reconfigures_with_logging_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_LOGGING_DIR", raising=False)
    first = LoggingSettings(dir=str(tmp_path / "first"), level="debug")
    second = LoggingSettings(dir=str(tmp_path / "second"), level="debug")
    setup_file_logging(first)
    error_log = TranscriptParserErrorLog("phase")
    old_handler = error_log.logger.handlers[0]

    setup_file_logging(second)
    error_log.log_unknown_block(1, "session", "future", {"type": "future"})

    assert getattr(old_handler, "stream", None) is None
    assert error_log.log_path == Path(second.dir) / "phase-parser-error.log"
    assert "Unknown block type: future" in error_log.log_path.read_text()
    assert (
        "Unknown block type: future" not in (Path(first.dir) / "phase-parser-error.log").read_text()
    )


def test_log_unknown_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOBBY_LOGGING_DIR", str(tmp_path / "logs"))
    cli_name = "test-cli-unknown"
    error_log = TranscriptParserErrorLog(cli_name)

    raw = {"type": "weird", "data": "value"}
    error_log.log_unknown_block(10, "session-2", "weird", raw)

    content = error_log.log_path.read_text()
    assert "line:10" in content
    assert "session:session-2" in content
    assert "Unknown block type: weird" in content
    assert json.dumps(raw) in content


def test_renderer_represents_unknown_block_without_parser_error_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    rendered = render_transcript([msg], session_id="session-3", error_log=error_log)

    block = rendered[0].content_blocks[0]
    assert block.type == "unknown"
    assert block.block_type == "magic_block"
    assert block.raw == {"type": "magic_block", "extra": "data"}
    if error_log.log_path.exists():
        assert "Unknown block type" not in error_log.log_path.read_text()


def test_unknown_block_message_preserves_raw_for_render_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_unknown_block_message_uses_content_or_fallback() -> None:
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
def test_classify_decode_failure(raw_text: str, expected: str) -> None:
    try:
        json.loads(raw_text)
    except json.JSONDecodeError as exc:
        error = exc
    else:
        error = json.JSONDecodeError("synthetic non-object JSON", raw_text, 0)

    assert _classify_decode_failure(raw_text, error) == expected


def test_parser_routes_decode_failures_by_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    class MockParser(BaseTranscriptParser):
        def parse_line(self, line: str, index: int) -> Any:
            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                self.error_log.log_decode_failure(index, self.session_id, line, e)
                return None

    parser = MockParser("mock-cli-decode", session_id="session-4")
    parser.parse_line('{"valid": "json"}', 1)  # parses fine — nothing logged
    parser.parse_line('{"truncated": ', 2)  # truncated -> INFO
    parser.parse_line("not json at all", 3)  # non_json -> DEBUG (suppressed)
    parser.parse_line("   ", 4)  # empty -> silent

    content = parser.error_log.log_path.read_text()
    # Only the truncated partial write survives the INFO-pinned logger.
    assert "line:2" in content
    assert "session:session-4" in content
    assert "Malformed line (truncated)" in content
    assert '{"truncated":' in content
    # Junk routes to DEBUG and stays out of parser-error.log.
    assert "line:3" not in content
    assert "not json at all" not in content
    # Empty lines never log.
    assert "line:4" not in content


def test_log_decode_failure_non_object_is_non_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    error_log = TranscriptParserErrorLog("test-cli-nonobject")

    # A decoded-but-not-an-object line (error=None) is non_json -> DEBUG, suppressed.
    error_log.log_decode_failure(5, "session-9", "[1, 2, 3]", None)

    content = error_log.log_path.read_text()
    assert "line:5" not in content
    assert "[1, 2, 3]" not in content


def test_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = LoggingSettings(
        dir=str(tmp_path / "logs"),
        max_size_mb=2,
        backup_count=3,
    )
    monkeypatch.setenv("GOBBY_LOGGING_DIR", config.dir)
    setup_file_logging(config)
    cli_name = "test-cli-rotation"
    error_log = TranscriptParserErrorLog(cli_name)

    from logging.handlers import RotatingFileHandler

    handler = error_log.logger.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.baseFilename == str(error_log.log_path)
    assert handler.maxBytes == 2 * 1024 * 1024
    assert handler.backupCount == 3
