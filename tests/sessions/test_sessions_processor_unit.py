"""
Unit tests for SessionMessageProcessor.

Tests edge cases, error handling, and branch coverage not covered
by integration tests.
"""

import asyncio
import json
from collections.abc import Iterable
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest

from gobby.sessions.message_stats import MessageStats
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcript_index import (
    build_index_from_file,
    load_index_sidecar,
    persist_index_sidecar,
)
from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage
from gobby.sessions.transcripts.typed_json import TypedJsonTranscriptParser
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_db():
    """Create a mock database."""
    return MagicMock()


@pytest.fixture
def processor(mock_db):
    """Create a processor with mocked dependencies."""
    return SessionMessageProcessor(mock_db, poll_interval=0.1)


def test_usage_has_tokens_rejects_bool_counts(processor) -> None:
    usage = MagicMock()
    usage.input_tokens = True
    usage.output_tokens = 0
    usage.cache_creation_tokens = 0
    usage.cache_read_tokens = 0
    message = MagicMock(usage=usage)

    assert processor._usage_has_tokens(message) is False


def _codex_response_message(role: str, text: str) -> str:
    block_type = "output_text" if role == "assistant" else "input_text"
    return (
        json.dumps(
            {
                "timestamp": "2026-04-20T04:05:07.572Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": block_type, "text": text}],
                },
            }
        )
        + "\n"
    )


def _codex_function_call(name: str, call_id: str) -> str:
    return (
        json.dumps(
            {
                "timestamp": "2026-04-20T04:05:07.572Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": name,
                    "arguments": "{}",
                    "call_id": call_id,
                },
            }
        )
        + "\n"
    )


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

    def test_register_session_replaces_changed_transcript_path(self, processor, tmp_path) -> None:
        first_transcript = tmp_path / "first.jsonl"
        second_transcript = tmp_path / "second.jsonl"
        first_transcript.touch()
        second_transcript.touch()
        processor.register_session("session-1", str(first_transcript))
        original_parser = processor._parsers["session-1"]
        processor._byte_offsets["session-1"] = 42

        processor.register_session("session-1", str(second_transcript))

        assert processor._active_sessions["session-1"] == str(second_transcript)
        assert processor._parsers["session-1"] is not original_parser
        assert "session-1" not in processor._byte_offsets

    def test_register_session_transcript_not_found(self, mock_db, tmp_path, caplog) -> None:
        """Register when the file doesn't exist yet (Codex writes rollout
        shortly after session_start). Must still register — the poll loop's
        existence check handles missing files and picks the file up on its
        next pass once Codex writes it."""
        mock_session_manager = MagicMock()
        processor = SessionMessageProcessor(
            mock_db, poll_interval=0.1, session_manager=mock_session_manager
        )
        nonexistent = tmp_path / "nonexistent.jsonl"

        with caplog.at_level("DEBUG"):
            processor.register_session("session-1", str(nonexistent))

        # Registered for monitoring — poll loop handles the file appearing later.
        assert "session-1" in processor._active_sessions
        assert "session-1" in processor._parsers
        # Should NOT overwrite transcript_path or mark processed — the DB's
        # authoritative transcript_path remains untouched.
        mock_session_manager.update.assert_not_called()
        mock_session_manager.mark_transcript_processed.assert_not_called()

    def test_register_session_with_different_sources(self, processor, tmp_path) -> None:
        """Register should use appropriate parser for each source."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        # Test different source types
        processor.register_session("claude-session", str(transcript), source="claude")
        processor.register_session("qwen-session", str(transcript), source="qwen")
        processor.register_session("codex-session", str(transcript), source="codex")

        assert "claude-session" in processor._parsers
        assert "qwen-session" in processor._parsers
        assert "codex-session" in processor._parsers

    def test_register_session_hydrates_matching_sidecar(self, mock_db, tmp_path) -> None:
        """Registration should resume byte offset, message index, stats, and appender."""
        session_manager = MagicMock()
        processor = SessionMessageProcessor(mock_db, session_manager=session_manager)
        transcript = tmp_path / "rollout.jsonl"
        transcript.write_text(
            _codex_response_message("user", "hello")
            + _codex_response_message("assistant", "let me check")
            + _codex_function_call("read", "call_1"),
            encoding="utf-8",
        )
        st = transcript.stat()
        index = build_index_from_file(
            str(transcript), "codex", "sid", mtime_ns=st.st_mtime_ns, size=st.st_size
        )
        index.parser_state = {"pending_tool_search_use_ids": ["call_resume"]}
        persist_index_sidecar(str(transcript), index)

        processor.register_session("sid", str(transcript), source="codex")

        assert processor._byte_offsets["sid"] == st.st_size
        assert processor._message_indices["sid"] == (index.next_parser_index or 0) - 1
        assert processor._stats["sid"] == index.session_stats
        appender = processor._index_appenders["sid"]
        assert appender.index.parsed_message_count == index.parsed_message_count
        assert appender._next_start_index == index.next_parser_index
        assert appender._next_raw_line_no == index.next_raw_line_no
        assert appender._safe_to_start_event == index.safe_to_start_event
        assert appender._state.current_message is not None
        assert appender._state.current_message.role == "assistant"
        assert "call_1" in appender._state.pending_tool_calls
        assert processor._parsers["sid"].snapshot_state() == index.parser_state
        session_manager.touch.assert_not_called()
        assert index.session_stats is not None
        session_manager.update_stats.assert_called_once_with(
            "sid",
            message_count=index.session_stats["message_count"],
            turn_count=index.session_stats["turn_count"],
            tool_call_count=index.session_stats["tool_call_count"],
            last_assistant_content=index.session_stats["last_assistant_content"],
        )

    def test_register_session_with_legacy_sidecar_skips_stats_update(
        self, mock_db, tmp_path
    ) -> None:
        session_manager = MagicMock()
        processor = SessionMessageProcessor(mock_db, session_manager=session_manager)
        transcript = tmp_path / "legacy-rollout.jsonl"
        transcript.write_text(
            _codex_response_message("user", "hello")
            + _codex_response_message("assistant", "cached"),
            encoding="utf-8",
        )
        st = transcript.stat()
        index = build_index_from_file(
            str(transcript), "codex", "sid", mtime_ns=st.st_mtime_ns, size=st.st_size
        )
        persist_index_sidecar(str(transcript), index)
        sidecar = tmp_path / "legacy-rollout.jsonl.gobby-index.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload.pop("session_stats", None)
        sidecar.write_text(json.dumps(payload), encoding="utf-8")

        processor.register_session("sid", str(transcript), source="codex")

        assert processor._byte_offsets["sid"] == st.st_size
        assert "sid" not in processor._stats
        assert "sid" in processor._stats_hydration_skipped
        session_manager.update_stats.assert_not_called()
        session_manager.touch.assert_not_called()

    @pytest.mark.asyncio
    async def test_append_after_sidecar_hydration_continues_assistant_group(
        self, mock_db, tmp_path
    ) -> None:
        session_manager = MagicMock()
        processor = SessionMessageProcessor(mock_db, session_manager=session_manager)
        transcript = tmp_path / "continuation.jsonl"
        transcript.write_text(
            _codex_response_message("user", "hello")
            + _codex_response_message("assistant", "first"),
            encoding="utf-8",
        )
        initial_stat = transcript.stat()
        index = build_index_from_file(
            str(transcript),
            "codex",
            "sid",
            mtime_ns=initial_stat.st_mtime_ns,
            size=initial_stat.st_size,
        )
        persist_index_sidecar(str(transcript), index)

        processor.register_session("sid", str(transcript), source="codex")
        assert index.session_stats is not None
        initial_groups = index.total_groups
        initial_count = index.session_stats["message_count"]

        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(_codex_response_message("assistant", "second"))

        await processor._process_session("sid", str(transcript))

        stats = processor._stats["sid"]
        assert stats["message_count"] == initial_count + 1
        assert stats["last_assistant_content"] == "second"
        assert processor._index_appenders["sid"].index.total_groups == initial_groups
        latest = load_index_sidecar(
            str(transcript),
            "codex",
            "sid",
            seek_mode="byte",
            mtime_ns=transcript.stat().st_mtime_ns,
            size=transcript.stat().st_size,
        )
        assert latest is not None
        assert latest.total_groups == initial_groups
        assert latest.session_stats == stats

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

        await wait_for_async_condition(
            lambda: "Error in SessionMessageProcessor loop" in caplog.text,
            description="processor loop error log",
        )

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
        assert processor.message_manager.get_state.call_count == 0
        assert not processor.message_manager.get_state.called

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
        assert processor.message_manager.store_messages.call_count == 0
        assert not processor.message_manager.store_messages.called

    @pytest.mark.asyncio
    async def test_process_session_revives_expired_terminal_on_new_lines(
        self, mock_db, tmp_path
    ) -> None:
        """Transcript activity revives a false-expired terminal session."""
        session_manager = MagicMock()
        processor = SessionMessageProcessor(mock_db, session_manager=session_manager)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type": "unknown"}\n')

        processor.register_session("session-1", str(transcript))
        mock_parser = MagicMock()
        mock_parser.parse_lines = MagicMock(return_value=[])
        processor._parsers["session-1"] = mock_parser

        await processor._process_session("session-1", str(transcript))

        assert processor._active_sessions["session-1"] == str(transcript)
        session_manager.revive_expired_terminal_session.assert_called_once_with("session-1")

    @pytest.mark.asyncio
    async def test_process_session_read_error(self, processor, tmp_path, caplog):
        """Should handle file read errors gracefully."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        processor._active_sessions["session-1"] = str(transcript)
        processor._parsers["session-1"] = MagicMock()
        processor.message_manager = AsyncMock()
        processor.message_manager.get_state = AsyncMock(return_value=None)

        # Make the file unreadable by patching aiofiles.open.
        with patch(
            "gobby.sessions.processor_transcripts.aiofiles.open",
            side_effect=PermissionError("Permission denied"),
        ):
            with caplog.at_level("ERROR"):
                await processor._process_session("session-1", str(transcript))

        assert "Error reading transcript" in caplog.text
        assert processor.message_manager.store_messages.call_count == 0

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
        assert processor.message_manager.store_messages.call_count == 0
        assert not processor.message_manager.store_messages.called

    @pytest.mark.asyncio
    async def test_flush_session_processes_unterminated_final_line(self, processor, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(_codex_response_message("user", "test").rstrip("\n"))
        processor.register_session("session-1", str(transcript), source="codex")

        result = await processor.flush_session("session-1")

        assert result.flushed is True
        assert result.error is None
        assert processor._stats["session-1"]["message_count"] == 1
        assert processor._byte_offsets["session-1"] == transcript.stat().st_size

    @pytest.mark.asyncio
    async def test_flush_session_reports_unregistered_session(self, processor):
        processor._process_session = AsyncMock()

        result = await processor.flush_session("unknown-session")

        assert result.flushed is False
        assert result.error == "session is not registered"
        processor._process_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flush_session_contains_processing_errors(self, processor, tmp_path, caplog):
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()
        processor.register_session("session-1", str(transcript))
        processor._process_session = AsyncMock(side_effect=RuntimeError("boom"))

        with caplog.at_level("ERROR"):
            result = await processor.flush_session("session-1")

        assert result.flushed is False
        assert result.error == "boom"
        processor._process_session.assert_awaited_once_with(
            "session-1", str(transcript), at_eof=True
        )
        assert "Failed to flush session transcript" in caplog.text

    @pytest.mark.asyncio
    async def test_concurrent_poll_and_flush_do_not_double_process_jsonl(
        self, processor, tmp_path
    ) -> None:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(_codex_response_message("user", "test"))
        processor.register_session("session-1", str(transcript), source="codex")
        batch_entered = asyncio.Event()
        release_batch = asyncio.Event()
        second_batch_entered = asyncio.Event()
        original_process_batch = processor._process_parsed_batch
        batch_count = 0

        async def blocked_process_batch(
            session_id: str, messages: list[ParsedMessage]
        ) -> MessageStats:
            nonlocal batch_count
            batch_count += 1
            if batch_count > 1:
                second_batch_entered.set()
            batch_entered.set()
            await asyncio.wait_for(release_batch.wait(), timeout=1)
            return await original_process_batch(session_id, messages)

        processor._process_parsed_batch = blocked_process_batch

        async with asyncio.TaskGroup() as tasks:
            poll_task = tasks.create_task(processor._process_all_sessions())
            await asyncio.wait_for(batch_entered.wait(), timeout=1)
            flush_task = tasks.create_task(processor.flush_session("session-1"))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second_batch_entered.wait(), timeout=0.05)
            release_batch.set()

        assert poll_task.result() is None
        assert flush_task.result().flushed is True
        assert batch_count == 1
        assert processor._stats["session-1"]["message_count"] == 1
        assert processor._message_indices["session-1"] == 0
        assert processor._index_appenders["session-1"].index.raw_record_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_poll_and_flush_do_not_double_process_json(
        self, processor, tmp_path
    ) -> None:
        transcript = tmp_path / "session.json"
        transcript.write_text(
            json.dumps(
                {
                    "sessionId": "abc",
                    "messages": [
                        {
                            "id": "1",
                            "timestamp": "2024-01-01T10:00:00Z",
                            "type": "user",
                            "content": "Hello",
                        }
                    ],
                }
            )
        )
        processor.register_session("session-1", str(transcript), source="qwen")
        batch_entered = asyncio.Event()
        release_batch = asyncio.Event()
        second_batch_entered = asyncio.Event()
        original_process_batch = processor._process_parsed_batch
        batch_count = 0

        async def blocked_process_batch(
            session_id: str, messages: list[ParsedMessage]
        ) -> MessageStats:
            nonlocal batch_count
            batch_count += 1
            if batch_count > 1:
                second_batch_entered.set()
            batch_entered.set()
            await asyncio.wait_for(release_batch.wait(), timeout=1)
            return await original_process_batch(session_id, messages)

        processor._process_parsed_batch = blocked_process_batch

        async with asyncio.TaskGroup() as tasks:
            poll_task = tasks.create_task(processor._process_all_sessions())
            await asyncio.wait_for(batch_entered.wait(), timeout=1)
            flush_task = tasks.create_task(processor.flush_session("session-1"))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second_batch_entered.wait(), timeout=0.05)
            release_batch.set()

        assert poll_task.result() is None
        assert flush_task.result().flushed is True
        assert batch_count == 1
        assert processor._stats["session-1"]["message_count"] == 1
        assert processor._message_indices["session-1"] == 0

    @pytest.mark.asyncio
    async def test_unregister_during_processing_does_not_resurrect_state(
        self, processor, tmp_path
    ) -> None:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(_codex_response_message("user", "test"))
        processor.register_session("session-1", str(transcript), source="codex")
        batch_entered = asyncio.Event()
        release_batch = asyncio.Event()
        original_process_batch = processor._process_parsed_batch

        async def blocked_process_batch(
            session_id: str, messages: list[ParsedMessage]
        ) -> MessageStats:
            batch_entered.set()
            await asyncio.wait_for(release_batch.wait(), timeout=1)
            return await original_process_batch(session_id, messages)

        processor._process_parsed_batch = blocked_process_batch

        processing_task = asyncio.create_task(processor._process_all_sessions())
        await asyncio.wait_for(batch_entered.wait(), timeout=1)
        processor.unregister_session("session-1")
        release_batch.set()
        await asyncio.wait_for(processing_task, timeout=1)

        state_maps = (
            processor._active_sessions,
            processor._parsers,
            processor._last_mtime,
            processor._stats,
            processor._byte_offsets,
            processor._message_indices,
            processor._index_appenders,
            processor._render_states,
            processor._processing_locks,
        )
        assert all("session-1" not in state for state in state_maps)

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
        assert processor.message_manager.store_messages.call_count == 0
        assert not processor.message_manager.store_messages.called

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
    async def test_process_session_advances_past_malformed_timestamp(self, processor, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": "malformed timestamp"},
                    "timestamp": {"unexpected": "shape"},
                }
            )
            + "\n"
        )
        processor.register_session("session-1", str(transcript))

        await processor._process_session("session-1", str(transcript))
        first_stats = processor._stats["session-1"].copy()
        await processor._process_session("session-1", str(transcript))

        assert processor._byte_offsets["session-1"] == transcript.stat().st_size
        assert processor._stats["session-1"] == first_stats

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

    @pytest.mark.asyncio
    async def test_process_session_resets_state_when_transcript_shrinks(
        self, processor, tmp_path
    ) -> None:
        transcript = tmp_path / "shrinking.jsonl"
        transcript.write_text(
            _codex_response_message("user", "first message with extra text")
            + _codex_response_message("assistant", "old assistant response with extra text"),
            encoding="utf-8",
        )
        processor.register_session("session-1", str(transcript), source="codex")
        await processor._process_session("session-1", str(transcript))
        old_parser = processor._parsers["session-1"]
        old_appender = processor._index_appenders["session-1"]

        replacement = _codex_response_message("user", "new")
        transcript.write_text(replacement, encoding="utf-8")
        await processor._process_session("session-1", str(transcript))

        assert processor._parsers["session-1"] is not old_parser
        assert processor._index_appenders["session-1"] is not old_appender
        assert processor._byte_offsets["session-1"] == len(replacement.encode())
        st = transcript.stat()
        index = load_index_sidecar(
            str(transcript),
            "codex",
            "session-1",
            seek_mode="byte",
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
        )
        assert index is not None
        assert index.parsed_message_count == 1
        assert index.boundaries[0].byte_start == 0

    @pytest.mark.asyncio
    async def test_process_session_resets_sidecar_when_transcript_is_replaced(
        self, processor, tmp_path
    ) -> None:
        transcript = tmp_path / "replaced.jsonl"
        transcript.write_text(_codex_response_message("user", "old"), encoding="utf-8")
        processor.register_session("session-1", str(transcript), source="codex")
        await processor._process_session("session-1", str(transcript))
        old_parser = processor._parsers["session-1"]
        old_appender = processor._index_appenders["session-1"]

        replacement_content = _codex_response_message(
            "user", "replacement content that is longer than old"
        ) + _codex_response_message("assistant", "replacement assistant response")
        replacement = tmp_path / "replacement.tmp"
        replacement.write_text(replacement_content, encoding="utf-8")
        replacement.replace(transcript)
        await processor._process_session("session-1", str(transcript))

        assert processor._parsers["session-1"] is not old_parser
        assert processor._index_appenders["session-1"] is not old_appender
        assert processor._byte_offsets["session-1"] == len(replacement_content.encode())
        st = transcript.stat()
        index = load_index_sidecar(
            str(transcript),
            "codex",
            "session-1",
            seek_mode="byte",
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
        )
        assert index is not None
        assert index.parsed_message_count == 2
        assert index.boundaries[0].byte_start == 0

    @pytest.mark.asyncio
    async def test_process_session_records_plain_transcript_observations(
        self, processor, tmp_path
    ) -> None:
        transcript = tmp_path / "transcript.jsonl"
        line = (
            '{"type": "user", "message": {"content": "hello"}, '
            '"timestamp": "2024-01-01T10:00:00Z"}\n'
        )
        transcript.write_text(line)

        processor.register_session("session-1", str(transcript))
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
        processor._render_and_broadcast_messages = AsyncMock()

        await processor._process_session("session-1", str(transcript))

        processor._render_and_broadcast_messages.assert_awaited_once_with(
            "session-1",
            [parsed_msg],
            record_observations=True,
        )
        assert processor._byte_offsets["session-1"] == len(line)
        assert processor._message_indices["session-1"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure_method",
        ["_persist_usage_events", "_render_and_broadcast_messages"],
    )
    async def test_process_session_retries_failed_batch_without_desync(
        self,
        processor,
        tmp_path,
        failure_method: str,
    ) -> None:
        transcript = tmp_path / "transcript.jsonl"
        first_line = '{"type": "unknown", "value": 1}\n'
        second_line = '{"type": "unknown", "value": 2}\n'
        third_line = '{"type": "unknown", "value": 3}\n'
        transcript.write_text(first_line)
        processor.register_session("session-1", str(transcript))
        original_appender = processor._index_appenders["session-1"]

        def parse_lines(lines: list[str], *, start_index: int) -> list[ParsedMessage]:
            return [
                ParsedMessage(
                    index=start_index + offset,
                    role="user",
                    content=line,
                    content_type="text",
                    tool_name=None,
                    tool_input=None,
                    tool_result=None,
                    timestamp=datetime.now(),
                    raw_json={},
                    message_id=f"message-{start_index + offset}",
                )
                for offset, line in enumerate(lines)
            ]

        parser = MagicMock()
        parser.parse_lines.side_effect = parse_lines
        processor._parsers["session-1"] = parser
        processor._persist_usage_events = AsyncMock()
        processor._render_and_broadcast_messages = AsyncMock()
        failed_call = getattr(processor, failure_method)
        failed_call.side_effect = RuntimeError("mid-batch failure")

        with pytest.raises(RuntimeError, match="mid-batch failure"):
            await processor._process_session("session-1", str(transcript))

        assert "session-1" not in processor._byte_offsets
        assert "session-1" not in processor._message_indices
        assert "session-1" not in processor._stats
        assert processor._index_appenders["session-1"] is original_appender
        assert original_appender.index.raw_record_count == 0

        failed_call.side_effect = None
        transcript.write_text(first_line + second_line)
        await processor._process_session("session-1", str(transcript))

        assert processor._byte_offsets["session-1"] == len(first_line + second_line)
        assert processor._message_indices["session-1"] == 1
        assert processor._stats["session-1"]["message_count"] == 2
        assert processor._index_appenders["session-1"].index.raw_record_count == 2

        transcript.write_text(first_line + second_line + third_line)
        await processor._process_session("session-1", str(transcript))

        assert [call.kwargs["start_index"] for call in parser.parse_lines.call_args_list] == [
            0,
            0,
            2,
        ]
        persisted_batches = processor._persist_usage_events.await_args_list[-2:]
        assert [[message.index for message in call.args[1]] for call in persisted_batches] == [
            [0, 1],
            [2],
        ]
        assert processor._message_indices["session-1"] == 2
        assert processor._stats["session-1"]["message_count"] == 3

    @pytest.mark.asyncio
    async def test_render_failure_does_not_commit_render_state(self, processor) -> None:
        message = ParsedMessage(
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
        processor.websocket_server = MagicMock()
        processor._broadcast_rendered_session_message = AsyncMock(
            side_effect=RuntimeError("broadcast failed")
        )

        with pytest.raises(RuntimeError, match="broadcast failed"):
            await processor._render_and_broadcast_messages("session-1", [message])

        assert "session-1" not in processor._render_states

        processor._broadcast_rendered_session_message.side_effect = None
        await processor._render_and_broadcast_messages("session-1", [message])

        assert "session-1" in processor._render_states


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
    async def test_tts_feed_failure_does_not_block_websocket_broadcast(self, mock_db, tmp_path):
        """Attached TTS failures should not stop transcript message broadcasts."""
        mock_ws_server = MagicMock()
        mock_ws_server.broadcast = AsyncMock()
        mock_ws_server.feed_attached_session_tts = AsyncMock(side_effect=RuntimeError("tts down"))

        processor = SessionMessageProcessor(mock_db, websocket_server=mock_ws_server)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "assistant", "message": {"content": "hello"}, "timestamp": "2024-01-01T10:00:00Z"}\n'
        )
        processor.register_session("session-1", str(transcript))

        timestamp = datetime(2024, 1, 1, 10, 0, 0)
        parsed_msg = ParsedMessage(
            index=0,
            role="assistant",
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

        with patch("gobby.sessions.processor.inc_counter") as inc_counter:
            await processor._process_session("session-1", str(transcript))

        mock_ws_server.feed_attached_session_tts.assert_awaited()
        mock_ws_server.broadcast.assert_awaited_once()
        broadcast_payload = mock_ws_server.broadcast.await_args.args[0]
        assert broadcast_payload["type"] == "session_message"
        assert broadcast_payload["message"]["content"] == "hello"
        inc_counter.assert_called_once_with("tts_feed_failures_total")

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
        assert mock_session_manager.update_model.call_count == 1
        assert mock_session_manager.update_model.call_args is not None

    @pytest.mark.asyncio
    async def test_process_session_persists_codex_token_usage(
        self, mock_db, tmp_path, monkeypatch
    ) -> None:
        """Codex token_count records should update the context pie source fields."""

        class FakeTokenEventStore:
            def __init__(self, _db: object) -> None:
                self.records: list[object] = []

            def get_session_totals(self, _session_id: str) -> dict[str, int]:
                return {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                }

            def record(self, event: object) -> bool:
                self.records.append(event)
                return True

        store = FakeTokenEventStore(mock_db)
        monkeypatch.setattr(
            "gobby.sessions.processor.TokenEventStore",
            lambda _db: store,
        )

        mock_session_manager = MagicMock()
        session = MagicMock()
        session.project_id = "proj-1"
        session.source = "codex"
        session.context_window = None
        session.model = None
        mock_session_manager.get.return_value = session

        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()
        mock_ws.broadcast_token_event = AsyncMock()
        mock_ws.broadcast_session_usage_updated = AsyncMock()

        processor = SessionMessageProcessor(
            mock_db,
            websocket_server=mock_ws,
            session_manager=mock_session_manager,
        )
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type": "event_msg"}\n')

        parsed_msg = ParsedMessage(
            index=0,
            role="assistant",
            content="",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={"payload": {"info": {"model_context_window": 258400}}},
            usage=TokenUsage(
                input_tokens=11392,
                output_tokens=498,
                cache_creation_tokens=0,
                cache_read_tokens=93568,
            ),
            model="gpt-5.5",
            message_id="token-count-1",
        )
        mock_parser = MagicMock()
        mock_parser.parse_lines.return_value = [parsed_msg]
        processor._active_sessions["session-1"] = str(transcript)
        processor._parsers["session-1"] = mock_parser

        await processor._process_session("session-1", str(transcript))

        assert store.records[0].input_tokens == 11392
        assert store.records[0].cache_read_tokens == 93568
        assert store.records[0].context_window == 258400
        mock_session_manager.update_usage.assert_called_once_with(
            session_id="session-1",
            input_tokens=11392,
            output_tokens=498,
            cache_creation_tokens=0,
            cache_read_tokens=93568,
            context_window=258400,
            model="gpt-5.5",
        )
        mock_session_manager.update_context_usage.assert_called_once()
        snapshot = mock_session_manager.update_context_usage.call_args.args[1]
        assert snapshot.context_used_tokens == 104960
        assert snapshot.raw_prompt_footprint == 104960
        assert snapshot.uncached_prompt_tokens == 11392
        assert snapshot.cache_read_tokens == 93568
        assert snapshot.context_usage_ratio == pytest.approx(104960 / 258400)
        assert snapshot.source == "codex"
        usage_payload = mock_ws.broadcast_session_usage_updated.await_args.args[0]
        assert usage_payload["usage_input_tokens"] == 11392
        assert usage_payload["context_used_tokens"] == 104960
        assert usage_payload["last_prompt_input_tokens"] == 104960
        assert usage_payload["last_prompt_uncached_input_tokens"] == 11392
        assert usage_payload["context_window"] == 258400
        mock_ws.broadcast_token_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_token_usage_refreshes_session_without_token_broadcast(
        self, mock_db, monkeypatch
    ) -> None:
        """Duplicate token events still refresh session totals and context snapshots."""

        class FakeTokenEventStore:
            def __init__(self, _db: object) -> None:
                pass

            def get_session_totals(self, _session_id: str) -> dict[str, int]:
                return {
                    "input_tokens": 11392,
                    "output_tokens": 498,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 93568,
                }

            def record(self, _event: object) -> bool:
                return False

        monkeypatch.setattr(
            "gobby.sessions.processor.TokenEventStore",
            lambda _db: FakeTokenEventStore(_db),
        )

        mock_session_manager = MagicMock()
        session = MagicMock()
        session.project_id = "proj-1"
        session.source = "codex"
        session.context_window = None
        session.model = None
        mock_session_manager.get.return_value = session

        mock_ws = MagicMock()
        mock_ws.broadcast_token_event = AsyncMock()
        mock_ws.broadcast_session_usage_updated = AsyncMock()

        processor = SessionMessageProcessor(
            mock_db,
            websocket_server=mock_ws,
            session_manager=mock_session_manager,
        )
        parsed_msg = ParsedMessage(
            index=0,
            role="assistant",
            content="",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={"payload": {"info": {"model_context_window": 258400}}},
            usage=TokenUsage(
                input_tokens=11392,
                output_tokens=498,
                cache_creation_tokens=0,
                cache_read_tokens=93568,
            ),
            model="gpt-5.5",
            message_id="token-count-1",
        )

        await processor._persist_usage_events("session-1", [parsed_msg])

        mock_session_manager.update_usage.assert_called_once_with(
            session_id="session-1",
            input_tokens=11392,
            output_tokens=498,
            cache_creation_tokens=0,
            cache_read_tokens=93568,
            context_window=258400,
            model="gpt-5.5",
        )
        mock_session_manager.update_context_usage.assert_called_once()
        usage_payload = mock_ws.broadcast_session_usage_updated.await_args.args[0]
        assert usage_payload["usage_input_tokens"] == 11392
        assert usage_payload["context_used_tokens"] == 104960
        mock_ws.broadcast_token_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_usage_events_ignores_session_lookup_db_errors(
        self,
        mock_db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_session_manager = MagicMock()
        mock_session_manager.get.side_effect = psycopg.OperationalError("db down")
        processor = SessionMessageProcessor(mock_db, session_manager=mock_session_manager)
        parsed_msg = ParsedMessage(
            index=0,
            role="assistant",
            content="Done",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={},
            usage=TokenUsage(
                input_tokens=1,
                output_tokens=2,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
            model="codex-test",
        )

        with caplog.at_level("DEBUG", logger="gobby.sessions.processor_usage"):
            await processor._persist_usage_events("session-1", [parsed_msg])

        assert "Failed to load session session-1 for token usage" in caplog.text
        mock_session_manager.update_usage.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_session_records_grok_window_only_snapshot(
        self,
        mock_db,
        tmp_path,
        monkeypatch,
    ) -> None:
        """Window-only providers should update normalized metadata without token events."""

        class FakeTokenEventStore:
            def __init__(self, _db: object) -> None:
                pass

            def get_session_totals(self, _session_id: str) -> dict[str, int]:
                return {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                }

            def record(self, _event: object) -> bool:
                raise AssertionError("window-only snapshots must not write token events")

        monkeypatch.setattr(
            "gobby.sessions.processor.TokenEventStore",
            lambda _db: FakeTokenEventStore(_db),
        )
        mock_session_manager = MagicMock()
        session = MagicMock()
        session.project_id = "proj-1"
        session.source = "grok"
        session.context_window = None
        session.model = None
        mock_session_manager.get.return_value = session
        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()
        mock_ws.broadcast_token_event = AsyncMock()
        mock_ws.broadcast_session_usage_updated = AsyncMock()
        processor = SessionMessageProcessor(
            mock_db,
            websocket_server=mock_ws,
            session_manager=mock_session_manager,
        )
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type": "event_msg"}\n')
        parsed_msg = ParsedMessage(
            index=0,
            role="assistant",
            content="",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={"params": {"update": {"totalContextTokens": 512000}}},
            model="grok-build",
            message_id="grok-window",
        )
        mock_parser = MagicMock()
        mock_parser.parse_lines.return_value = [parsed_msg]
        processor._active_sessions["session-1"] = str(transcript)
        processor._parsers["session-1"] = mock_parser

        await processor._process_session("session-1", str(transcript))

        mock_session_manager.update_usage.assert_not_called()
        mock_session_manager.update_context_usage.assert_called_once()
        snapshot = mock_session_manager.update_context_usage.call_args.args[1]
        assert snapshot.source == "grok"
        assert snapshot.context_window == 512000
        assert snapshot.context_used_tokens is None
        assert snapshot.context_usage_ratio is None
        usage_payload = mock_ws.broadcast_session_usage_updated.await_args.args[0]
        assert usage_payload["context_window"] == 512000
        assert usage_payload["context_used_tokens"] is None
        mock_ws.broadcast_token_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_session_records_grok_token_snapshot(
        self,
        mock_db,
        tmp_path,
        monkeypatch,
    ) -> None:
        """Grok messages with real usage select snapshot_from_token_usage, not window-only."""

        class FakeTokenEventStore:
            def __init__(self, _db: object) -> None:
                pass

            def get_session_totals(self, _session_id: str) -> dict[str, int]:
                return {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                }

            def record(self, _event: object) -> bool:
                return True

        monkeypatch.setattr(
            "gobby.sessions.processor.TokenEventStore",
            lambda _db: FakeTokenEventStore(_db),
        )

        from gobby.sessions import context_usage as _ctx

        token_spy = MagicMock(side_effect=_ctx.snapshot_from_token_usage)
        window_spy = MagicMock(side_effect=_ctx.snapshot_from_window_metadata)
        monkeypatch.setattr("gobby.sessions.processor.snapshot_from_token_usage", token_spy)
        monkeypatch.setattr("gobby.sessions.processor.snapshot_from_window_metadata", window_spy)

        mock_session_manager = MagicMock()
        session = MagicMock()
        session.project_id = "proj-1"
        session.source = "grok"
        session.context_window = None
        session.model = None
        mock_session_manager.get.return_value = session
        mock_ws = MagicMock()
        mock_ws.broadcast = AsyncMock()
        mock_ws.broadcast_token_event = AsyncMock()
        mock_ws.broadcast_session_usage_updated = AsyncMock()
        processor = SessionMessageProcessor(
            mock_db,
            websocket_server=mock_ws,
            session_manager=mock_session_manager,
        )
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type": "event_msg"}\n')
        parsed_msg = ParsedMessage(
            index=0,
            role="assistant",
            content="Done",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={"params": {"update": {"totalContextTokens": 512000}}},
            usage=TokenUsage(
                input_tokens=1_500,
                output_tokens=250,
                cache_creation_tokens=500,
                cache_read_tokens=8_000,
            ),
            model="grok-build",
            message_id="grok-token",
        )
        mock_parser = MagicMock()
        mock_parser.parse_lines.return_value = [parsed_msg]
        processor._active_sessions["session-1"] = str(transcript)
        processor._parsers["session-1"] = mock_parser

        await processor._process_session("session-1", str(transcript))

        # Real per-message usage drives _usage_has_tokens -> True, selecting the
        # token-based snapshot and never the window-only fallback.
        token_spy.assert_called_once()
        window_spy.assert_not_called()
        mock_ws.broadcast_token_event.assert_awaited_once()
        mock_session_manager.update_usage.assert_called_once()
        snapshot = mock_session_manager.update_context_usage.call_args.args[1]
        assert snapshot.source == "grok"
        assert snapshot.context_window == 512000
        assert snapshot.context_used_tokens == 10_000
        assert snapshot.confidence == "reported"

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
        assert mock_session_manager.update_model.call_count == 0
        assert not mock_session_manager.update_model.called


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

        processor.register_session("session-1", str(transcript), source="qwen")
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
    """Tests for _process_json_session (Qwen typed-JSON format)."""

    @pytest.mark.asyncio
    async def test_process_json_session_basic(self, mock_db, tmp_path) -> None:
        """Should parse and store messages from a Qwen JSON session file."""
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

        processor.register_session("session-1", str(transcript), source="qwen")
        processor._parsers["session-1"] = TypedJsonTranscriptParser(cli_name="typed-json")

        await processor._process_json_session("session-1", str(transcript))

        # Should have processed 2 messages
        assert processor._stats["session-1"]["message_count"] == 2
        assert processor._message_indices["session-1"] == 1

        # Should track mtime
        assert "session-1" in processor._last_mtime

    @pytest.mark.asyncio
    async def test_process_json_session_retry_does_not_double_accumulate_stats(
        self, mock_db, tmp_path
    ) -> None:
        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "session.json"
        transcript.write_text(
            json.dumps(
                {
                    "sessionId": "abc",
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
            )
        )
        processor.register_session("session-1", str(transcript), source="qwen")
        processor._parsers["session-1"] = TypedJsonTranscriptParser(cli_name="typed-json")
        processor._persist_usage_events = AsyncMock()
        processor._render_and_broadcast_messages = AsyncMock(
            side_effect=RuntimeError("render failed")
        )

        with pytest.raises(RuntimeError, match="render failed"):
            await processor._process_json_session("session-1", str(transcript))

        assert "session-1" not in processor._stats
        assert "session-1" not in processor._message_indices
        assert "session-1" not in processor._last_mtime

        processor._render_and_broadcast_messages.side_effect = None
        await processor._process_json_session("session-1", str(transcript))

        assert processor._stats["session-1"]["message_count"] == 2
        assert processor._message_indices["session-1"] == 1
        assert "session-1" in processor._last_mtime

    @pytest.mark.asyncio
    async def test_process_json_session_passes_parser_source_to_normalizer(
        self,
        mock_db,
        tmp_path,
    ) -> None:
        import json

        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "session.json"
        transcript.write_text(
            json.dumps(
                {
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
            )
        )
        processor.register_session("session-1", str(transcript), source="qwen")
        processor._parsers["session-1"] = TypedJsonTranscriptParser(cli_name="typed-json")
        seen_sources: list[str | None] = []

        def normalize(records: Iterable[object], source: str | None) -> list[object]:
            seen_sources.append(source)
            return list(records)

        with patch(
            "gobby.sessions.processor_transcripts.normalize_transcript_records",
            side_effect=normalize,
        ):
            await processor._process_json_session("session-1", str(transcript))

        assert seen_sources == ["typed-json"]

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

        processor.register_session("session-1", str(transcript), source="qwen")
        processor.message_manager = AsyncMock()

        # Set mtime to current file mtime (pretend we already processed)
        processor._last_mtime["session-1"] = os.path.getmtime(str(transcript))

        await processor._process_json_session("session-1", str(transcript))

        # Should not call get_state since we skipped
        processor.message_manager.get_state.assert_not_called()
        assert processor.message_manager.get_state.call_count == 0
        assert not processor.message_manager.get_state.called

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

        processor.register_session("session-1", str(transcript), source="qwen")
        processor._parsers["session-1"] = TypedJsonTranscriptParser(cli_name="typed-json")
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
        processor.register_session("session-1", "/nonexistent/file.json", source="qwen")
        processor.message_manager = AsyncMock()

        await processor._process_json_session("session-1", "/nonexistent/file.json")
        processor.message_manager.get_state.assert_not_called()
        assert processor.message_manager.get_state.call_count == 0
        assert not processor.message_manager.get_state.called

    @pytest.mark.asyncio
    async def test_process_json_session_invalid_json(self, mock_db, tmp_path, caplog) -> None:
        """Should handle invalid JSON gracefully."""
        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "bad.json"
        transcript.write_text("not valid json {{{")

        processor.register_session("session-1", str(transcript), source="qwen")
        processor.message_manager = AsyncMock()

        await processor._process_json_session("session-1", str(transcript))
        assert "Error reading JSON transcript" in caplog.text

    @pytest.mark.asyncio
    async def test_process_json_session_wrong_parser_type(self, mock_db, tmp_path, caplog) -> None:
        """Should warn when source has no JSON session parser."""
        import json

        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "session.json"
        transcript.write_text(json.dumps({"sessionId": "x", "messages": []}))

        # Register with claude parser (wrong for JSON)
        processor.register_session("session-1", str(transcript), source="claude")
        processor.message_manager = AsyncMock()
        processor.message_manager.get_state = AsyncMock(return_value=None)

        await processor._process_json_session("session-1", str(transcript))
        assert "No JSON-session transcript parser" in caplog.text

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

        processor.register_session("session-1", str(transcript), source="qwen")
        processor._parsers["session-1"] = TypedJsonTranscriptParser(cli_name="typed-json")

        await processor._process_session("session-1", str(transcript))

        # Should have processed via JSON path
        assert processor._stats["session-1"]["message_count"] == 1
        assert processor._message_indices["session-1"] == 0
        assert "session-1" in processor._last_mtime


def _codex_event_msg(payload_type: str, **payload_extra) -> str:
    """Build a Codex event_msg envelope line."""
    payload = {"type": payload_type, **payload_extra}
    return (
        json.dumps(
            {
                "timestamp": "2026-04-20T04:05:07.572Z",
                "type": "event_msg",
                "payload": payload,
            }
        )
        + "\n"
    )


class TestCodexMcpTranscriptProcessing:
    """Codex MCP transcript records are parsed for history without hook dispatch."""

    @pytest.mark.asyncio
    async def test_mcp_begin_and_end_do_not_dispatch_workflow_hooks(
        self, mock_db, tmp_path
    ) -> None:
        hook_manager = MagicMock()
        processor = SessionMessageProcessor(mock_db, hook_manager=hook_manager)

        transcript = tmp_path / "rollout.jsonl"
        invocation = {
            "server": "gobby",
            "tool": "get_tool_schema",
            "arguments": {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "session_id": "#2995",
            },
        }
        transcript.write_text(
            _codex_event_msg("mcp_tool_call_begin", call_id="call_1", invocation=invocation)
            + _codex_event_msg(
                "mcp_tool_call_end",
                call_id="call_1",
                invocation=invocation,
                duration={"secs": 0, "nanos": 18_695_333},
                result={"Ok": {"content": [{"type": "text", "text": "{...}"}]}},
            )
        )

        processor.register_session("sid", str(transcript), source="codex")

        await processor._process_session("sid", str(transcript))

        hook_manager.handle.assert_not_called()
        assert processor._byte_offsets["sid"] == transcript.stat().st_size

    @pytest.mark.asyncio
    async def test_byte_offset_prevents_reprocessing_mcp_lifecycle_lines(
        self, mock_db, tmp_path
    ) -> None:
        hook_manager = MagicMock()
        processor = SessionMessageProcessor(mock_db, hook_manager=hook_manager)

        transcript = tmp_path / "rollout.jsonl"
        invocation = {
            "server": "gobby",
            "tool": "get_tool_schema",
            "arguments": {"server_name": "gobby-tasks", "tool_name": "create_task"},
        }
        transcript.write_text(
            _codex_event_msg("mcp_tool_call_begin", call_id="call_1", invocation=invocation)
        )

        processor.register_session("sid", str(transcript), source="codex")

        await processor._process_session("sid", str(transcript))
        first_offset = processor._byte_offsets["sid"]

        await processor._process_session("sid", str(transcript))

        hook_manager.handle.assert_not_called()
        assert processor._byte_offsets["sid"] == first_offset

    @pytest.mark.asyncio
    async def test_registration_survives_missing_transcript_then_picks_up(
        self, mock_db, tmp_path
    ) -> None:
        """Codex writes its rollout slightly after session_start fires."""
        hook_manager = MagicMock()
        processor = SessionMessageProcessor(mock_db, hook_manager=hook_manager)

        transcript = tmp_path / "future-rollout.jsonl"
        assert not transcript.exists()

        processor.register_session("sid", str(transcript), source="codex")
        assert "sid" in processor._active_sessions

        await processor._process_session("sid", str(transcript))
        hook_manager.handle.assert_not_called()

        transcript.write_text(
            _codex_event_msg(
                "mcp_tool_call_begin",
                call_id="call_late",
                invocation={
                    "server": "gobby",
                    "tool": "get_tool_schema",
                    "arguments": {"server_name": "gobby-tasks", "tool_name": "create_task"},
                },
            )
        )

        await processor._process_session("sid", str(transcript))

        hook_manager.handle.assert_not_called()
        assert processor._byte_offsets["sid"] == transcript.stat().st_size

    @pytest.mark.asyncio
    async def test_mcp_end_error_does_not_dispatch_workflow_hooks(self, mock_db, tmp_path) -> None:
        hook_manager = MagicMock()
        processor = SessionMessageProcessor(mock_db, hook_manager=hook_manager)

        transcript = tmp_path / "rollout.jsonl"
        invocation = {
            "server": "gobby",
            "tool": "list_tools",
            "arguments": {"server_name": "context7"},
        }
        transcript.write_text(
            _codex_event_msg(
                "mcp_tool_call_end",
                call_id="call_err",
                invocation=invocation,
                result={"Err": "transport closed"},
            )
        )

        processor.register_session("sid", str(transcript), source="codex")
        await processor._process_session("sid", str(transcript))

        hook_manager.handle.assert_not_called()
        assert processor._byte_offsets["sid"] == transcript.stat().st_size


class TestExtractNativeTitles:
    """Tests for _extract_native_titles — intercepts session_title messages
    before stats/render, updates the session title, and returns non-title msgs."""

    def _make_title_msg(
        self,
        content: str,
        index: int = 0,
        *,
        source: str | None = None,
    ) -> ParsedMessage:
        return ParsedMessage(
            index=index,
            role="system",
            content=content,
            content_type="session_title",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={},
            source=source,
        )

    def _make_text_msg(self, content: str, index: int = 1) -> ParsedMessage:
        return ParsedMessage(
            index=index,
            role="user",
            content=content,
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={},
        )

    def test_extracts_title_and_returns_non_title_messages(self, mock_db) -> None:
        processor = SessionMessageProcessor(mock_db)
        session_manager = MagicMock()
        session = MagicMock()
        session.title = ""
        session.title_source = ""
        session_manager.get.return_value = session
        processor.session_manager = session_manager

        messages = [
            self._make_title_msg("Fix auth bug", index=0),
            self._make_text_msg("Hello world", index=1),
        ]
        result = processor._extract_native_titles("sid", messages)

        assert len(result) == 1
        assert result[0].content_type == "text"
        session_manager.update_title.assert_called_once_with(
            "sid", "Fix auth bug", title_source="native"
        )

    def test_claude_title_slug_dashes_become_spaces(self, mock_db) -> None:
        processor = SessionMessageProcessor(mock_db)
        session_manager = MagicMock()
        session = MagicMock()
        session.title = ""
        session.title_source = ""
        session_manager.get.return_value = session
        processor.session_manager = session_manager

        messages = [
            self._make_title_msg(
                "check-gobby-logs-for-tmux-warnings",
                source="claude",
            )
        ]
        result = processor._extract_native_titles("sid", messages)

        assert result == []
        session_manager.update_title.assert_called_once_with(
            "sid", "check gobby logs for tmux warnings", title_source="native"
        )

    def test_title_without_message_source_uses_parser_source(self, mock_db) -> None:
        processor = SessionMessageProcessor(mock_db)
        session_manager = MagicMock()
        session = MagicMock()
        session.title = ""
        session.title_source = ""
        session_manager.get.return_value = session
        processor.session_manager = session_manager
        processor._parsers["sid"] = MagicMock(cli_name="claude")

        result = processor._extract_native_titles(
            "sid",
            [self._make_title_msg("check-gobby-logs-for-tmux-warnings")],
        )

        assert result == []
        session_manager.update_title.assert_called_once_with(
            "sid", "check gobby logs for tmux warnings", title_source="native"
        )

    def test_skips_when_session_manager_is_none(self, mock_db) -> None:
        processor = SessionMessageProcessor(mock_db)
        processor.session_manager = None

        messages = [self._make_title_msg("Fix auth bug")]
        result = processor._extract_native_titles("sid", messages)

        assert len(result) == 0

    def test_skips_when_title_is_manual(self, mock_db) -> None:
        processor = SessionMessageProcessor(mock_db)
        session_manager = MagicMock()
        session = MagicMock()
        session.title = "My manual title"
        session.title_source = "manual"
        session_manager.get.return_value = session
        processor.session_manager = session_manager

        messages = [self._make_title_msg("Native title")]
        result = processor._extract_native_titles("sid", messages)

        assert len(result) == 0
        session_manager.update_title.assert_not_called()

    def test_skips_when_title_is_llm(self, mock_db) -> None:
        processor = SessionMessageProcessor(mock_db)
        session_manager = MagicMock()
        session = MagicMock()
        session.title = "LLM digest title"
        session.title_source = "llm"
        session_manager.get.return_value = session
        processor.session_manager = session_manager

        messages = [self._make_title_msg("Native title")]
        result = processor._extract_native_titles("sid", messages)

        assert len(result) == 0
        session_manager.update_title.assert_not_called()

    def test_rejects_garbage_native_title(self, mock_db) -> None:
        """Droid sessionTitle that's a response dump is rejected by normalize_native_title."""
        processor = SessionMessageProcessor(mock_db)
        session_manager = MagicMock()
        session = MagicMock()
        session.title = ""
        session.title_source = ""
        session_manager.get.return_value = session
        processor.session_manager = session_manager

        garbage = "I will help. <function_calls> stuff " * 20
        messages = [self._make_title_msg(garbage)]
        result = processor._extract_native_titles("sid", messages)

        assert len(result) == 0
        session_manager.update_title.assert_not_called()

    def test_no_title_messages_returns_unchanged(self, mock_db) -> None:
        processor = SessionMessageProcessor(mock_db)
        processor.session_manager = MagicMock()

        messages = [self._make_text_msg("Hello", index=0), self._make_text_msg("World", index=1)]
        result = processor._extract_native_titles("sid", messages)

        assert result == messages

    def test_empty_messages_returns_empty(self, mock_db) -> None:
        processor = SessionMessageProcessor(mock_db)
        result = processor._extract_native_titles("sid", [])
        assert result == []

    def test_latest_title_wins_for_multiple_title_messages(self, mock_db) -> None:
        """Claude may emit multiple ai-title updates; the last one wins."""
        processor = SessionMessageProcessor(mock_db)
        session_manager = MagicMock()
        session = MagicMock()
        session.title = "Old title"
        session.title_source = "native"
        session_manager.get.return_value = session
        processor.session_manager = session_manager

        messages = [
            self._make_title_msg("First title", index=0),
            self._make_title_msg("Updated title", index=1),
        ]
        result = processor._extract_native_titles("sid", messages)

        assert len(result) == 0
        session_manager.update_title.assert_called_once_with(
            "sid", "Updated title", title_source="native"
        )

    @pytest.mark.asyncio
    async def test_native_title_db_error_preserves_retry_state(self, mock_db, tmp_path) -> None:
        processor = SessionMessageProcessor(mock_db)
        transcript = tmp_path / "transcript.jsonl"
        line = (
            '{"type": "assistant", "message": {"content": []}, '
            '"timestamp": "2024-01-01T10:00:00Z"}\n'
        )
        transcript.write_text(line)
        processor.register_session("sid", str(transcript))

        parsed_msg = self._make_title_msg("Native title", index=0)
        mock_parser = MagicMock()
        mock_parser.parse_lines = MagicMock(return_value=[parsed_msg])
        processor._parsers["sid"] = mock_parser
        session_manager = MagicMock()
        session = MagicMock()
        session.title = ""
        session.title_source = ""
        session_manager.get.return_value = session
        session_manager.update_title.side_effect = psycopg.Error("db unavailable")
        processor.session_manager = session_manager

        with pytest.raises(psycopg.Error):
            await processor._process_session("sid", str(transcript))

        assert processor._byte_offsets.get("sid", 0) == 0
        assert processor._message_indices.get("sid", -1) == -1
