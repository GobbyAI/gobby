"""
Unit tests for SessionMessageProcessor.

Tests edge cases, error handling, and branch coverage not covered
by integration tests.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcripts.base import ParsedMessage

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_db():
    """Create a mock database."""
    return MagicMock()


@pytest.fixture
def processor(mock_db):
    """Create a processor with mocked dependencies."""
    return SessionMessageProcessor(mock_db, poll_interval=0.1)


class TestProcessorLifecycle:
    """Tests for start/stop lifecycle methods."""

    @pytest.mark.asyncio
    async def test_start_when_already_running(self, processor):
        """Start should be a no-op when already running."""
        # Start once
        await processor.start()
        assert processor._running is True
        first_task = processor._task

        # Start again - should return early without creating new task
        await processor.start()
        assert processor._running is True
        assert processor._task is first_task  # Same task, not replaced

        await processor.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_running(self, processor):
        """Stop should handle the case when not running."""
        # Processor never started
        assert processor._running is False
        assert processor._task is None

        # Stop should complete without error
        await processor.stop()
        assert processor._running is False
        assert processor._task is None

    @pytest.mark.asyncio
    async def test_stop_when_running(self, processor):
        """Stop should cancel the task and clean up."""
        await processor.start()
        assert processor._running is True
        assert processor._task is not None

        await processor.stop()
        assert processor._running is False
        assert processor._task is None

    @pytest.mark.asyncio
    async def test_stop_handles_cancelled_error(self, processor):
        """Stop should gracefully handle CancelledError from task."""
        await processor.start()

        # Stop should handle the CancelledError internally
        await processor.stop()
        assert processor._running is False


class TestSessionRegistration:
    """Tests for session registration and unregistration."""

    def test_register_session_already_registered(self, processor, tmp_path) -> None:
        """Registering the same session twice should be a no-op."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        # First registration
        processor.register_session("session-1", str(transcript))
        assert "session-1" in processor._active_sessions
        assert "session-1" in processor._parsers

        original_parser = processor._parsers["session-1"]

        # Second registration - should return early
        processor.register_session("session-1", str(transcript))
        assert processor._parsers["session-1"] is original_parser  # Not replaced

    def test_register_session_transcript_not_found(self, mock_db, tmp_path, caplog) -> None:
        """Register should skip monitoring but NOT overwrite transcript_path."""
        mock_session_manager = MagicMock()
        processor = SessionMessageProcessor(
            mock_db, poll_interval=0.1, session_manager=mock_session_manager
        )
        nonexistent = tmp_path / "nonexistent.jsonl"

        with caplog.at_level("DEBUG"):
            processor.register_session("session-1", str(nonexistent))

        # Should NOT be registered for active monitoring
        assert "session-1" not in processor._active_sessions
        assert "session-1" not in processor._parsers
        # Should NOT overwrite transcript_path or mark processed —
        # TranscriptReader will re-derive the path at read time
        mock_session_manager.update.assert_not_called()
        mock_session_manager.mark_transcript_processed.assert_not_called()

    def test_register_session_with_different_sources(self, processor, tmp_path) -> None:
        """Register should use appropriate parser for each source."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        # Test different source types
        processor.register_session("claude-session", str(transcript), source="claude")
        processor.register_session("gemini-session", str(transcript), source="gemini")
        processor.register_session("codex-session", str(transcript), source="codex")

        assert "claude-session" in processor._parsers
        assert "gemini-session" in processor._parsers
        assert "codex-session" in processor._parsers

    def test_unregister_session_existing(self, processor, tmp_path) -> None:
        """Unregister should remove session and parser."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        processor.register_session("session-1", str(transcript))
        assert "session-1" in processor._active_sessions
        assert "session-1" in processor._parsers

        processor.unregister_session("session-1")
        assert "session-1" not in processor._active_sessions
        assert "session-1" not in processor._parsers

    def test_unregister_session_not_registered(self, processor) -> None:
        """Unregister should be a no-op for non-existent session."""
        # Should not raise
        processor.unregister_session("nonexistent")
        assert "nonexistent" not in processor._active_sessions

    def test_unregister_session_missing_parser(self, processor, tmp_path) -> None:
        """Unregister should handle case where parser is missing."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        processor.register_session("session-1", str(transcript))

        # Manually remove parser (edge case)
        del processor._parsers["session-1"]

        # Should still unregister without error
        processor.unregister_session("session-1")
        assert "session-1" not in processor._active_sessions


class TestProcessingLoop:
    """Tests for the main processing loop."""

    @pytest.mark.asyncio
    async def test_loop_handles_exception(self, processor, caplog):
        """Loop should continue after exception in _process_all_sessions."""
        # Make _process_all_sessions raise an exception
        processor._process_all_sessions = AsyncMock(side_effect=Exception("Test error"))

        await processor.start()

        # Give the loop time to execute and encounter the error
        await asyncio.sleep(0.15)

        assert "Error in SessionMessageProcessor loop" in caplog.text
        assert processor._running  # Loop should continue

        await processor.stop()

    @pytest.mark.asyncio
    async def test_process_all_sessions_handles_session_error(self, processor, tmp_path, caplog):
        """_process_all_sessions should continue processing other sessions on error."""
        transcript1 = tmp_path / "t1.jsonl"
        transcript2 = tmp_path / "t2.jsonl"
        transcript1.touch()
        transcript2.touch()

        processor.register_session("session-1", str(transcript1))
        processor.register_session("session-2", str(transcript2))

        # Mock _process_session to fail for session-1 but succeed for session-2
        original_process = processor._process_session

        async def mock_process(session_id, path):
            if session_id == "session-1":
                raise Exception("Session 1 error")
            return await original_process(session_id, path)

        processor._process_session = mock_process

        with caplog.at_level("ERROR"):
            await processor._process_all_sessions()

        assert "Failed to process session session-1" in caplog.text


class TestProcessSession:
    """Tests for _process_session method."""

    @pytest.mark.asyncio
    async def test_process_session_transcript_not_exists(self, processor):
        """Should return early if transcript file doesn't exist."""
        processor._active_sessions["session-1"] = "/nonexistent/path.jsonl"
        processor._parsers["session-1"] = MagicMock()
        processor.message_manager = AsyncMock()

        await processor._process_session("session-1", "/nonexistent/path.jsonl")

        # get_state should not be called since we returned early
        processor.message_manager.get_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_session_no_parser(self, processor, tmp_path):
        """Should return early if parser is missing."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type": "user", "message": {"content": "test"}}\n')

        processor._active_sessions["session-1"] = str(transcript)
        # No parser registered
        processor.message_manager = AsyncMock()
        processor.message_manager.get_state = AsyncMock(return_value=None)

        await processor._process_session("session-1", str(transcript))

        # store_messages should not be called since we returned early (no parser)
        processor.message_manager.store_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_session_read_error(self, processor, tmp_path, caplog):
        """Should handle file read errors gracefully."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        processor._active_sessions["session-1"] = str(transcript)
        processor._parsers["session-1"] = MagicMock()
        processor.message_manager = AsyncMock()
        processor.message_manager.get_state = AsyncMock(return_value=None)

        # Make the file unreadable by patching open
        with patch("builtins.open", side_effect=PermissionError("Permission denied")):
            with caplog.at_level("ERROR"):
                await processor._process_session("session-1", str(transcript))

        assert "Error reading transcript" in caplog.text

    @pytest.mark.asyncio
    async def test_process_session_incomplete_line(self, processor, tmp_path):
        """Should not process incomplete lines (without newline)."""
        transcript = tmp_path / "transcript.jsonl"
        # Write an incomplete line (no trailing newline)
        with open(transcript, "w") as f:
            f.write('{"type": "user", "message": {"content": "test"}}')  # No \n

        processor.register_session("session-1", str(transcript))
        processor.message_manager = AsyncMock()
        processor.message_manager.get_state = AsyncMock(return_value=None)
        processor.message_manager.store_messages = AsyncMock()
        processor.message_manager.update_state = AsyncMock()

        await processor._process_session("session-1", str(transcript))

        # Should not store any messages (line is incomplete)
        processor.message_manager.store_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_session_no_new_lines(self, processor, tmp_path):
        """Should return early when no new lines to process."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()  # Empty file

        processor.register_session("session-1", str(transcript))
        processor.message_manager = AsyncMock()
        processor.message_manager.get_state = AsyncMock(return_value=None)
        processor.message_manager.store_messages = AsyncMock()

        await processor._process_session("session-1", str(transcript))

        # Should not call store_messages
        processor.message_manager.store_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_session_no_parsed_messages(self, processor, tmp_path):
        """Should update byte offset even when parser returns no messages."""
        transcript = tmp_path / "transcript.jsonl"
        # Write a line that will be parsed but might not produce a message
        transcript.write_text('{"type": "unknown"}\n')

        processor.register_session("session-1", str(transcript))

        # Mock parser to return empty list
        mock_parser = MagicMock()
        mock_parser.parse_lines = MagicMock(return_value=[])
        processor._parsers["session-1"] = mock_parser

        await processor._process_session("session-1", str(transcript))

        # Should advance byte offset even without messages
        assert processor._byte_offsets.get("session-1", 0) > 0
        # Message index should not have been set (no valid messages)
        assert "session-1" not in processor._message_indices

    @pytest.mark.asyncio
    async def test_process_session_with_existing_state(self, processor, tmp_path):
        """Should resume from last byte offset."""
        transcript = tmp_path / "transcript.jsonl"
        msg1 = '{"type": "user", "message": {"content": "msg1"}, "timestamp": "2024-01-01T10:00:00Z"}\n'
        msg2 = '{"type": "user", "message": {"content": "msg2"}, "timestamp": "2024-01-01T10:01:00Z"}\n'
        transcript.write_text(msg1 + msg2)

        processor.register_session("session-1", str(transcript))

        # Simulate state: already processed up to end of msg1
        processor._byte_offsets["session-1"] = len(msg1)
        processor._message_indices["session-1"] = 0

        # Mock parser
        mock_parser = MagicMock()
        parsed_msg = ParsedMessage(
            index=1,
            role="user",
            content="msg2",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={},
        )
        mock_parser.parse_lines = MagicMock(return_value=[parsed_msg])
        processor._parsers["session-1"] = mock_parser

        await processor._process_session("session-1", str(transcript))

        # Parser should only receive msg2 (starting from index 1)
        mock_parser.parse_lines.assert_called_once()
        call_args = mock_parser.parse_lines.call_args
        assert call_args[1]["start_index"] == 1

        # Message index should be updated to 1
        assert processor._message_indices["session-1"] == 1


class TestWebSocketBroadcast:
    """Tests for WebSocket broadcasting functionality."""

    @pytest.mark.asyncio
    async def test_broadcast_messages_to_websocket(self, mock_db, tmp_path):
        """Should broadcast parsed messages to WebSocket server."""
        mock_ws_server = MagicMock()
        mock_ws_server.broadcast = AsyncMock()

        processor = SessionMessageProcessor(mock_db, websocket_server=mock_ws_server)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "hello"}, "timestamp": "2024-01-01T10:00:00Z"}\n'
        )

        processor.register_session("session-1", str(transcript))

        # Mock message manager
        processor.message_manager = AsyncMock()
        processor.message_manager.get_state = AsyncMock(return_value=None)
        processor.message_manager.store_messages = AsyncMock()
        processor.message_manager.update_state = AsyncMock()

        # Mock parser
        timestamp = datetime(2024, 1, 1, 10, 0, 0)
        parsed_msg = ParsedMessage(
            index=0,
            role="user",
            content="hello",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=timestamp,
            raw_json={},
        )
        mock_parser = MagicMock()
        mock_parser.parse_lines = MagicMock(return_value=[parsed_msg])
        processor._parsers["session-1"] = mock_parser

        await processor._process_session("session-1", str(transcript))

        # Verify broadcast was called
        mock_ws_server.broadcast.assert_called_once()
        call_args = mock_ws_server.broadcast.call_args[0][0]
        assert call_args["type"] == "session_message"
        assert call_args["session_id"] == "session-1"
        assert call_args["message"]["content"] == "hello"
        assert call_args["message"]["role"] == "user"

    @pytest.mark.asyncio
    async def test_no_broadcast_without_websocket_server(self, mock_db, tmp_path):
        """Should skip broadcast when no WebSocket server is configured."""
        processor = SessionMessageProcessor(mock_db, websocket_server=None)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "hello"}, "timestamp": "2024-01-01T10:00:00Z"}\n'
        )

        processor.register_session("session-1", str(transcript))

        # Mock parser
        parsed_msg = ParsedMessage(
            index=0,
            role="user",
            content="hello",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={},
        )
        mock_parser = MagicMock()
        mock_parser.parse_lines = MagicMock(return_value=[parsed_msg])
        processor._parsers["session-1"] = mock_parser

        # Should complete without error (no broadcast)
        await processor._process_session("session-1", str(transcript))

        # Verify processing worked — stats updated and offset advanced
        assert processor._stats["session-1"]["message_count"] == 1
        assert processor._byte_offsets.get("session-1", 0) > 0


class TestMultipleMessages:
    """Tests for processing multiple messages."""

    @pytest.mark.asyncio
    async def test_process_multiple_messages_updates_last_index(self, mock_db, tmp_path):
        """Should update in-memory state with the last message index."""
        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "msg1"}, "timestamp": "2024-01-01T10:00:00Z"}\n'
            '{"type": "user", "message": {"content": "msg2"}, "timestamp": "2024-01-01T10:01:00Z"}\n'
            '{"type": "user", "message": {"content": "msg3"}, "timestamp": "2024-01-01T10:02:00Z"}\n'
        )

        processor.register_session("session-1", str(transcript))

        # Mock parser to return 3 messages
        parsed_messages = [
            ParsedMessage(
                index=i,
                role="user",
                content=f"msg{i + 1}",
                content_type="text",
                tool_name=None,
                tool_input=None,
                tool_result=None,
                timestamp=datetime.now(),
                raw_json={},
            )
            for i in range(3)
        ]
        mock_parser = MagicMock()
        mock_parser.parse_lines = MagicMock(return_value=parsed_messages)
        processor._parsers["session-1"] = mock_parser

        await processor._process_session("session-1", str(transcript))

        # Verify in-memory state was updated with last message index (2)
        assert processor._message_indices["session-1"] == 2
        assert processor._stats["session-1"]["message_count"] == 3


@pytest.mark.unit
class TestModelExtraction:
    """Tests for extracting and storing model from parsed messages."""

    @pytest.mark.asyncio
    async def test_process_session_captures_model(self, mock_db, tmp_path) -> None:
        """Should extract model from parsed messages and update session."""
        mock_session_manager = MagicMock()
        mock_session_manager.update_model = MagicMock()

        processor = SessionMessageProcessor(mock_db, session_manager=mock_session_manager)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "agent", "message": {"model": "claude-opus-4-5-20251101", "content": [{"type": "text", "text": "hello"}]}, "timestamp": "2024-01-01T10:00:00Z"}\n'
        )

        processor.register_session("session-1", str(transcript))

        # Mock message manager
        processor.message_manager = AsyncMock()
        processor.message_manager.get_state = AsyncMock(return_value=None)
        processor.message_manager.store_messages = AsyncMock()
        processor.message_manager.update_state = AsyncMock()

        # Create a parsed message with model
        parsed_msg = ParsedMessage(
            index=0,
            role="assistant",
            content="hello",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={},
            model="claude-opus-4-5-20251101",
        )
        mock_parser = MagicMock()
        mock_parser.parse_lines = MagicMock(return_value=[parsed_msg])
        processor._parsers["session-1"] = mock_parser

        await processor._process_session("session-1", str(transcript))

        # Verify session model was updated
        mock_session_manager.update_model.assert_called_once_with(
            "session-1", "claude-opus-4-5-20251101"
        )

    @pytest.mark.asyncio
    async def test_process_session_skips_model_update_when_none(self, mock_db, tmp_path) -> None:
        """Should not update model when parsed message has no model."""
        mock_session_manager = MagicMock()
        mock_session_manager.update_model = MagicMock()

        processor = SessionMessageProcessor(mock_db, session_manager=mock_session_manager)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "hello"}, "timestamp": "2024-01-01T10:00:00Z"}\n'
        )

        processor.register_session("session-1", str(transcript))

        # Mock message manager
        processor.message_manager = AsyncMock()
        processor.message_manager.get_state = AsyncMock(return_value=None)
        processor.message_manager.store_messages = AsyncMock()
        processor.message_manager.update_state = AsyncMock()

        # Create a parsed message without model
        parsed_msg = ParsedMessage(
            index=0,
            role="user",
            content="hello",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={},
            model=None,
        )
        mock_parser = MagicMock()
        mock_parser.parse_lines = MagicMock(return_value=[parsed_msg])
        processor._parsers["session-1"] = mock_parser

        await processor._process_session("session-1", str(transcript))

        # Verify session model was NOT updated
        mock_session_manager.update_model.assert_not_called()


class TestInitialization:
    """Tests for processor initialization."""

    def test_default_poll_interval(self, mock_db) -> None:
        """Should use default poll interval of 2.0 seconds."""
        processor = SessionMessageProcessor(mock_db)
        assert processor.poll_interval == 2.0

    def test_custom_poll_interval(self, mock_db) -> None:
        """Should accept custom poll interval."""
        processor = SessionMessageProcessor(mock_db, poll_interval=5.0)
        assert processor.poll_interval == 5.0

    def test_initial_state(self, mock_db) -> None:
        """Should initialize with empty state."""
        processor = SessionMessageProcessor(mock_db)
        assert processor._active_sessions == {}
        assert processor._parsers == {}
        assert processor._running is False
        assert processor._task is None

    def test_websocket_server_optional(self, mock_db) -> None:
        """Should accept optional WebSocket server."""
        mock_ws = MagicMock()
        processor = SessionMessageProcessor(mock_db, websocket_server=mock_ws)
        assert processor.websocket_server is mock_ws

        processor_no_ws = SessionMessageProcessor(mock_db)
        assert processor_no_ws.websocket_server is None

    def test_initial_state_includes_mtime(self, mock_db) -> None:
        """Should initialize with empty mtime tracking dict."""
        processor = SessionMessageProcessor(mock_db)
        assert processor._last_mtime == {}


class TestUnregisterCleansMtime:
    """Tests that unregister cleans up mtime tracking."""

    def test_unregister_removes_mtime(self, processor, tmp_path) -> None:
        """Unregister should clean up mtime tracking."""
        transcript = tmp_path / "transcript.json"
        transcript.touch()

        processor.register_session("session-1", str(transcript), source="gemini")
        processor._last_mtime["session-1"] = 12345.0

        processor.unregister_session("session-1")
        assert "session-1" not in processor._last_mtime

    def test_unregister_no_mtime_entry(self, processor, tmp_path) -> None:
        """Unregister should handle missing mtime entry gracefully."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        processor.register_session("session-1", str(transcript))
        # No mtime entry set
        processor.unregister_session("session-1")
        assert "session-1" not in processor._last_mtime


class TestProcessJsonSession:
    """Tests for _process_json_session (Gemini native JSON format)."""

    @pytest.mark.asyncio
    async def test_process_json_session_basic(self, mock_db, tmp_path) -> None:
        """Should parse and store messages from a Gemini JSON session file."""
        import json

        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "session-2024-01-01T10-00-abc12345.json"
        data = {
            "sessionId": "abc-12345",
            "messages": [
                {
                    "id": "1",
                    "timestamp": "2024-01-01T10:00:00Z",
                    "type": "user",
                    "content": "Hello",
                },
                {
                    "id": "2",
                    "timestamp": "2024-01-01T10:00:01Z",
                    "type": "gemini",
                    "content": "Hi there",
                },
            ],
        }
        transcript.write_text(json.dumps(data))

        processor.register_session("session-1", str(transcript), source="gemini")

        await processor._process_json_session("session-1", str(transcript))

        # Should have processed 2 messages
        assert processor._stats["session-1"]["message_count"] == 2
        assert processor._message_indices["session-1"] == 1

        # Should track mtime
        assert "session-1" in processor._last_mtime

    @pytest.mark.asyncio
    async def test_process_json_session_skips_unchanged(self, mock_db, tmp_path) -> None:
        """Should skip processing when file hasn't changed (mtime check)."""
        import json
        import os

        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "session.json"
        data = {
            "sessionId": "abc",
            "messages": [
                {
                    "id": "1",
                    "timestamp": "2024-01-01T10:00:00Z",
                    "type": "user",
                    "content": "Hello",
                },
            ],
        }
        transcript.write_text(json.dumps(data))

        processor.register_session("session-1", str(transcript), source="gemini")
        processor.message_manager = AsyncMock()

        # Set mtime to current file mtime (pretend we already processed)
        processor._last_mtime["session-1"] = os.path.getmtime(str(transcript))

        await processor._process_json_session("session-1", str(transcript))

        # Should not call get_state since we skipped
        processor.message_manager.get_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_json_session_incremental(self, mock_db, tmp_path) -> None:
        """Should only store new messages beyond last_message_index."""
        import json

        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "session.json"
        data = {
            "sessionId": "abc",
            "messages": [
                {
                    "id": "1",
                    "timestamp": "2024-01-01T10:00:00Z",
                    "type": "user",
                    "content": "First",
                },
                {
                    "id": "2",
                    "timestamp": "2024-01-01T10:00:01Z",
                    "type": "gemini",
                    "content": "Second",
                },
                {
                    "id": "3",
                    "timestamp": "2024-01-01T10:00:02Z",
                    "type": "user",
                    "content": "Third",
                },
            ],
        }
        transcript.write_text(json.dumps(data))

        processor.register_session("session-1", str(transcript), source="gemini")
        # Pretend we already processed up to index 1
        processor._message_indices["session-1"] = 1

        await processor._process_json_session("session-1", str(transcript))

        # Should only have processed 1 new message (Third, at index 2)
        assert processor._stats["session-1"]["message_count"] == 1
        assert processor._message_indices["session-1"] == 2

    @pytest.mark.asyncio
    async def test_process_json_session_file_not_found(self, mock_db) -> None:
        """Should return early when transcript file doesn't exist."""
        processor = SessionMessageProcessor(mock_db)
        processor.register_session("session-1", "/nonexistent/file.json", source="gemini")
        processor.message_manager = AsyncMock()

        await processor._process_json_session("session-1", "/nonexistent/file.json")
        processor.message_manager.get_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_json_session_invalid_json(self, mock_db, tmp_path, caplog) -> None:
        """Should handle invalid JSON gracefully."""
        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "bad.json"
        transcript.write_text("not valid json {{{")

        processor.register_session("session-1", str(transcript), source="gemini")
        processor.message_manager = AsyncMock()

        await processor._process_json_session("session-1", str(transcript))
        assert "Error reading JSON transcript" in caplog.text

    @pytest.mark.asyncio
    async def test_process_json_session_wrong_parser_type(self, mock_db, tmp_path, caplog) -> None:
        """Should warn when parser is not GeminiTranscriptParser."""
        import json

        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "session.json"
        transcript.write_text(json.dumps({"sessionId": "x", "messages": []}))

        # Register with claude parser (wrong for JSON)
        processor.register_session("session-1", str(transcript), source="claude")
        processor.message_manager = AsyncMock()
        processor.message_manager.get_state = AsyncMock(return_value=None)

        await processor._process_json_session("session-1", str(transcript))
        assert "No GeminiTranscriptParser" in caplog.text

    @pytest.mark.asyncio
    async def test_process_session_dispatches_to_json(self, mock_db, tmp_path) -> None:
        """_process_session should dispatch to _process_json_session for .json files."""
        import json

        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "session.json"
        data = {
            "sessionId": "abc",
            "messages": [
                {
                    "id": "1",
                    "timestamp": "2024-01-01T10:00:00Z",
                    "type": "user",
                    "content": "Hello",
                },
            ],
        }
        transcript.write_text(json.dumps(data))

        processor.register_session("session-1", str(transcript), source="gemini")

        await processor._process_session("session-1", str(transcript))

        # Should have processed via JSON path
        assert processor._stats["session-1"]["message_count"] == 1
        assert processor._message_indices["session-1"] == 0
        assert "session-1" in processor._last_mtime
