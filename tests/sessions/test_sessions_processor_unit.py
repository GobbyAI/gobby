"""
Unit tests for SessionMessageProcessor.

Tests edge cases, error handling, and branch coverage not covered
by integration tests.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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
from gobby.sessions.transcripts import PARSER_REGISTRY
from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.storage.token_events import TokenEvent
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_db() -> MagicMock:
    """Create a mock database."""
    return MagicMock()


@pytest.fixture
def processor(mock_db: MagicMock) -> SessionMessageProcessor:
    """Create a processor with mocked dependencies."""
    return SessionMessageProcessor(mock_db, poll_interval=0.1)


def test_usage_has_tokens_rejects_bool_counts(processor: SessionMessageProcessor) -> None:
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
    async def test_start_when_already_running(self, processor: SessionMessageProcessor) -> None:
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
    async def test_stop_when_not_running(self, processor: SessionMessageProcessor) -> None:
        """Stop should handle the case when not running."""
        # Processor never started
        assert processor._running is False
        assert processor._task is None

        # Stop should complete without error
        await processor.stop()
        assert processor._running is False
        assert processor._task is None

    @pytest.mark.asyncio
    async def test_stop_when_running(self, processor: SessionMessageProcessor) -> None:
        """Stop should cancel the task and clean up."""
        await processor.start()
        assert processor._running is True
        assert processor._task is not None

        await processor.stop()
        assert processor._running is False
        assert processor._task is None

    @pytest.mark.asyncio
    async def test_stop_handles_cancelled_error(self, processor: SessionMessageProcessor) -> None:
        """Stop should gracefully handle CancelledError from task."""
        await processor.start()

        # Stop should handle the CancelledError internally
        await processor.stop()
        assert processor._running is False


class TestSessionRegistration:
    """Tests for session registration and unregistration."""

    def test_register_session_already_registered(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
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

    def test_register_session_replaces_changed_transcript_path(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
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

    def test_register_session_transcript_not_found(
        self, mock_db: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
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

    def test_register_session_with_different_sources(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        """Register should use appropriate parser for each source."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        # Test different source types
        processor.register_session("claude-session", str(transcript), source="claude")
        processor.register_session("qwen-session", str(transcript), source="qwen")
        processor.register_session("codex-session", str(transcript), source="codex")
        assert "agy" in PARSER_REGISTRY
        processor.register_session("agy-session", str(transcript), source="agy")

        assert "claude-session" in processor._parsers
        assert "qwen-session" in processor._parsers
        assert "codex-session" in processor._parsers
        assert getattr(processor._parsers["agy-session"], "cli_name", None) == "agy"

    def test_agy_sidecar_admits_append_only_growth(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        from gobby.sessions.transcripts import PARSER_REGISTRY

        assert "agy" in PARSER_REGISTRY
        transcript = tmp_path / "transcript_full.jsonl"
        prefix = (
            json.dumps(
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-08-22T08:21:24Z",
                    "tool_calls": [
                        {"name": "run_command", "args": {"CommandLine": "pwd"}},
                    ],
                }
            )
            + "\n"
        )
        result = (
            json.dumps(
                {
                    "step_index": 3,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-08-22T08:21:26Z",
                    "content": "The command exited with code 0.\n",
                }
            )
            + "\n"
        )
        transcript.write_text(prefix, encoding="utf-8")
        st = transcript.stat()
        index = build_index_from_file(
            str(transcript), "agy", "sid", mtime_ns=st.st_mtime_ns, size=st.st_size
        )
        persist_index_sidecar(str(transcript), index)
        transcript.write_text(prefix + result, encoding="utf-8")

        processor.register_session("sid", str(transcript), source="agy")

        assert processor._byte_offsets["sid"] == st.st_size
        pending = processor._parsers["sid"].snapshot_state()
        assert pending

    def test_agy_sidecar_is_never_written_under_gemini(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        from gobby.paths import get_gobby_home
        from gobby.sessions.transcripts import PARSER_REGISTRY

        assert "agy" in PARSER_REGISTRY
        gemini = (
            tmp_path
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / "conv-1"
            / ".system_generated"
            / "logs"
        )
        gemini.mkdir(parents=True)
        transcript = gemini / "transcript_full.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "step_index": 1,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-08-22T08:21:24Z",
                    "content": "hello",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        st = transcript.stat()
        index = build_index_from_file(
            str(transcript), "agy", "sid", mtime_ns=st.st_mtime_ns, size=st.st_size
        )
        persist_index_sidecar(str(transcript), index)

        assert list(gemini.rglob("*.gobby-index.json")) == []
        cache = get_gobby_home() / "cache" / "transcript-indexes"
        assert any(cache.glob("*.gobby-index.json"))

    def test_register_qwen_json_creates_incremental_index_appender(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        transcript = tmp_path / "session.json"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "qwen-user-1",
                    "timestamp": "2026-07-17T01:00:00Z",
                    "message": {"role": "user", "parts": [{"text": "hi"}]},
                }
            )
            + "\n"
        )

        processor.register_session("qwen-session", str(transcript), source="qwen")

        assert "qwen-session" in processor._index_appenders

    def test_register_session_hydrates_matching_sidecar(
        self, mock_db: MagicMock, tmp_path: Path
    ) -> None:
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
        # Pre-resume calls are remembered as resolved ids -- shared on every
        # per-batch clone -- rather than as pending stubs that would be
        # deep-copied forever (#20875).
        assert appender._state.pending_tool_calls == {}
        assert "call_1" in appender._state.resolved_tool_call_ids
        assert processor._parsers["sid"].snapshot_state()["pending_tool_search_use_ids"] == [
            "call_resume"
        ]
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
        self, mock_db: MagicMock, tmp_path: Path
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
        from gobby.sessions.transcript_index_sidecar import _sidecar_path

        sidecar = Path(_sidecar_path(str(transcript)))
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
        self, mock_db: MagicMock, tmp_path: Path
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

    async def test_register_session_resumes_sidecar_after_append(
        self, mock_db: MagicMock, tmp_path: Path
    ) -> None:
        """Restart registration should process only bytes appended after the sidecar."""
        session_manager = MagicMock()
        transcript = tmp_path / "restart-continuation.jsonl"
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
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(_codex_response_message("assistant", "second"))

        processor = SessionMessageProcessor(mock_db, session_manager=session_manager)
        processor.register_session("sid", str(transcript), source="codex")

        assert processor._byte_offsets["sid"] == initial_stat.st_size
        await processor._process_session("sid", str(transcript))
        assert processor._stats["sid"]["last_assistant_content"] == "second"
        assert (
            processor._stats["sid"]["message_count"]
            == (index.session_stats or {}).get("message_count", 0) + 1
        )

    def test_register_session_rejects_replaced_append_candidate(
        self, mock_db: MagicMock, tmp_path: Path
    ) -> None:
        """A larger replacement file must not be mistaken for an appended rollout."""
        transcript = tmp_path / "replaced-before-restart.jsonl"
        transcript.write_text(_codex_response_message("user", "old"), encoding="utf-8")
        initial_stat = transcript.stat()
        index = build_index_from_file(
            str(transcript),
            "codex",
            "sid",
            mtime_ns=initial_stat.st_mtime_ns,
            size=initial_stat.st_size,
        )
        persist_index_sidecar(str(transcript), index)
        replacement = tmp_path / "replacement.tmp"
        replacement.write_text(
            _codex_response_message("user", "replacement content that is longer than old"),
            encoding="utf-8",
        )
        replacement.replace(transcript)

        processor = SessionMessageProcessor(mock_db)
        processor.register_session("sid", str(transcript), source="codex")

        assert "sid" not in processor._byte_offsets

    def test_register_session_rejects_in_place_rewrite_with_larger_size(
        self,
        mock_db: MagicMock,
        tmp_path: Path,
    ) -> None:
        transcript = tmp_path / "rewritten-before-restart.jsonl"
        transcript.write_text(_codex_response_message("user", "old"), encoding="utf-8")
        initial_stat = transcript.stat()
        index = build_index_from_file(
            str(transcript),
            "codex",
            "sid",
            mtime_ns=initial_stat.st_mtime_ns,
            size=initial_stat.st_size,
        )
        persist_index_sidecar(str(transcript), index)
        original_inode = transcript.stat().st_ino
        transcript.write_text(
            _codex_response_message(
                "user",
                "replacement content that is longer than the original transcript",
            ),
            encoding="utf-8",
        )
        assert transcript.stat().st_ino == original_inode

        processor = SessionMessageProcessor(mock_db)
        processor.register_session("sid", str(transcript), source="codex")

        assert "sid" not in processor._byte_offsets

    def test_unregister_session_existing(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        """Unregister should remove session and parser."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        processor.register_session("session-1", str(transcript))
        assert "session-1" in processor._active_sessions
        assert "session-1" in processor._parsers

        processor.unregister_session("session-1")
        assert "session-1" not in processor._active_sessions
        assert "session-1" not in processor._parsers

    def test_unregister_session_not_registered(self, processor: SessionMessageProcessor) -> None:
        """Unregister should be a no-op for non-existent session."""
        # Should not raise
        processor.unregister_session("nonexistent")
        assert "nonexistent" not in processor._active_sessions

    def test_unregister_session_missing_parser(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
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
    async def test_loop_handles_exception(
        self, processor: SessionMessageProcessor, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Loop should continue after exception in _process_all_sessions."""
        with patch.object(
            processor,
            "_process_all_sessions",
            new_callable=AsyncMock,
            side_effect=Exception("Test error"),
        ):
            await processor.start()

            await wait_for_async_condition(
                lambda: "Error in SessionMessageProcessor loop" in caplog.text,
                description="processor loop error log",
            )

            assert "Error in SessionMessageProcessor loop" in caplog.text
            assert processor._running  # Loop should continue

            await processor.stop()

    @pytest.mark.asyncio
    async def test_process_all_sessions_handles_session_error(
        self,
        processor: SessionMessageProcessor,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_process_all_sessions should continue processing other sessions on error."""
        transcript1 = tmp_path / "t1.jsonl"
        transcript2 = tmp_path / "t2.jsonl"
        transcript1.touch()
        transcript2.touch()

        processor.register_session("session-1", str(transcript1))
        processor.register_session("session-2", str(transcript2))

        # Mock _process_session to fail for session-1 but succeed for session-2
        original_process = processor._process_session

        async def mock_process(session_id: str, path: str) -> None:
            if session_id == "session-1":
                raise Exception("Session 1 error")
            return await original_process(session_id, path)

        with (
            patch.object(processor, "_process_session", side_effect=mock_process),
            caplog.at_level("ERROR"),
        ):
            await processor._process_all_sessions()

        assert "Failed to process session session-1" in caplog.text


class TestProcessSession:
    """Tests for _process_session method."""

    @pytest.mark.asyncio
    async def test_process_session_transcript_not_exists(
        self, processor: SessionMessageProcessor
    ) -> None:
        """Should return early if transcript file doesn't exist."""
        processor._active_sessions["session-1"] = "/nonexistent/path.jsonl"
        processor._parsers["session-1"] = MagicMock()
        await processor._process_session("session-1", "/nonexistent/path.jsonl")

        assert "session-1" not in processor._byte_offsets
        assert "session-1" not in processor._stats

    @pytest.mark.asyncio
    async def test_process_session_no_parser(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        """Should return early if parser is missing."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type": "user", "message": {"content": "test"}}\n')

        processor._active_sessions["session-1"] = str(transcript)
        # No parser registered
        await processor._process_session("session-1", str(transcript))

        assert "session-1" not in processor._byte_offsets
        assert "session-1" not in processor._stats

    @pytest.mark.asyncio
    async def test_process_session_revives_expired_terminal_on_new_lines(
        self, mock_db: MagicMock, tmp_path: Path
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
    async def test_process_session_read_error(
        self,
        processor: SessionMessageProcessor,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Should handle file read errors gracefully."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        processor._active_sessions["session-1"] = str(transcript)
        processor._parsers["session-1"] = MagicMock()
        # Make the file unreadable by patching aiofiles.open.
        with patch(
            "gobby.sessions.processor_transcripts.aiofiles.open",
            side_effect=PermissionError("Permission denied"),
        ):
            with caplog.at_level("ERROR"):
                await processor._process_session("session-1", str(transcript))

        assert "Error reading transcript" in caplog.text

    @pytest.mark.asyncio
    async def test_process_session_incomplete_line(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        """Should not process incomplete lines (without newline)."""
        transcript = tmp_path / "transcript.jsonl"
        # Write an incomplete line (no trailing newline)
        with open(transcript, "w") as f:
            f.write('{"type": "user", "message": {"content": "test"}}')  # No \n

        processor.register_session("session-1", str(transcript))
        await processor._process_session("session-1", str(transcript))

        assert processor._byte_offsets.get("session-1", 0) == 0
        assert "session-1" not in processor._stats

    @pytest.mark.asyncio
    async def test_flush_session_processes_unterminated_final_line(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(_codex_response_message("user", "test").rstrip("\n"))
        processor.register_session("session-1", str(transcript), source="codex")

        result = await processor.flush_session("session-1")

        assert result.flushed is True
        assert result.error is None
        assert processor._stats["session-1"]["message_count"] == 1
        assert processor._byte_offsets["session-1"] == transcript.stat().st_size

    @pytest.mark.asyncio
    async def test_flush_session_reports_unregistered_session(
        self, processor: SessionMessageProcessor
    ) -> None:
        with patch.object(processor, "_process_session", new_callable=AsyncMock) as process_session:
            result = await processor.flush_session("unknown-session")

        assert result.flushed is False
        assert result.error == "session is not registered"
        process_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconcile_codex_transcript_unregisters_temporary_registration(
        self,
        mock_db: MagicMock,
        tmp_path: Path,
    ) -> None:
        transcript = tmp_path / "reconcile.jsonl"
        transcript.touch()
        session_manager = MagicMock()
        session_manager.get.return_value = SimpleNamespace(
            source="codex",
            transcript_path=str(transcript),
        )
        processor = SessionMessageProcessor(mock_db, session_manager=session_manager)

        result = await processor.reconcile_codex_transcript("sid")

        assert result.flushed is True
        assert "sid" not in processor._active_sessions

    @pytest.mark.asyncio
    async def test_reconcile_codex_transcript_preserves_existing_registration(
        self,
        mock_db: MagicMock,
        tmp_path: Path,
    ) -> None:
        transcript = tmp_path / "registered-reconcile.jsonl"
        transcript.touch()
        processor = SessionMessageProcessor(mock_db)
        processor.register_session("sid", str(transcript), source="codex")

        result = await processor.reconcile_codex_transcript("sid")

        assert result.flushed is True
        assert processor._active_sessions["sid"] == str(transcript)

    @pytest.mark.asyncio
    async def test_flush_session_contains_processing_errors(
        self,
        processor: SessionMessageProcessor,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()
        processor.register_session("session-1", str(transcript))
        with (
            patch.object(
                processor,
                "_process_session",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ) as process_session,
            caplog.at_level("ERROR"),
        ):
            result = await processor.flush_session("session-1")

        assert result.flushed is False
        assert result.error == "boom"
        process_session.assert_awaited_once_with("session-1", str(transcript), at_eof=True)
        assert "Failed to flush session transcript" in caplog.text

    @pytest.mark.asyncio
    async def test_concurrent_poll_and_flush_do_not_double_process_jsonl(
        self, processor: SessionMessageProcessor, tmp_path: Path
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

        with patch.object(processor, "_process_parsed_batch", side_effect=blocked_process_batch):
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
    async def test_qwen_json_processes_appended_envelopes_incrementally(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        transcript = tmp_path / "session.json"
        first = json.dumps(
            {
                "type": "user",
                "uuid": "qwen-user-1",
                "timestamp": "2026-07-17T01:00:00Z",
                "message": {"role": "user", "parts": [{"text": "Hello"}]},
            }
        )
        second = json.dumps(
            {
                "type": "assistant",
                "uuid": "qwen-assistant-1",
                "timestamp": "2026-07-17T01:00:01Z",
                "message": {"role": "model", "parts": [{"text": "Hi"}]},
            }
        )
        transcript.write_text(first + "\n")
        processor.register_session("session-1", str(transcript), source="qwen")

        await processor._process_session("session-1", str(transcript))
        with transcript.open("a") as handle:
            handle.write(second + "\n")
        await processor._process_session("session-1", str(transcript))

        st = transcript.stat()
        index = load_index_sidecar(
            str(transcript),
            "qwen",
            "session-1",
            seek_mode="byte",
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
        )
        assert processor._stats["session-1"]["message_count"] == 2
        assert processor._message_indices["session-1"] == 1
        assert index is not None
        assert index.parsed_message_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_poll_and_flush_do_not_double_process_json(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        transcript = tmp_path / "session.json"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "qwen-user-1",
                    "timestamp": "2026-07-17T01:00:00Z",
                    "message": {"role": "user", "parts": [{"text": "Hello"}]},
                }
            )
            + "\n"
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

        with patch.object(processor, "_process_parsed_batch", side_effect=blocked_process_batch):
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
        self, processor: SessionMessageProcessor, tmp_path: Path
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

        with patch.object(processor, "_process_parsed_batch", side_effect=blocked_process_batch):
            processing_task = asyncio.create_task(processor._process_all_sessions())
            await asyncio.wait_for(batch_entered.wait(), timeout=1)
            processor.unregister_session("session-1")
            release_batch.set()
            await asyncio.wait_for(processing_task, timeout=1)

        state_maps = (
            processor._active_sessions,
            processor._parsers,
            processor._stats,
            processor._byte_offsets,
            processor._message_indices,
            processor._index_appenders,
            processor._render_states,
            processor._processing_locks,
        )
        assert all("session-1" not in state for state in state_maps)

    @pytest.mark.asyncio
    async def test_process_session_no_new_lines(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
        """Should return early when no new lines to process."""
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()  # Empty file

        processor.register_session("session-1", str(transcript))
        await processor._process_session("session-1", str(transcript))

        assert processor._byte_offsets.get("session-1", 0) == 0
        assert "session-1" not in processor._stats

    @pytest.mark.asyncio
    async def test_process_session_no_parsed_messages(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
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
    async def test_process_session_runs_index_append_on_db_executor(
        self,
        mock_db: MagicMock,
        tmp_path: Path,
    ) -> None:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type": "unknown"}\n')
        db_calls: list[str] = []

        async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            db_calls.append(func.__name__)
            return await asyncio.to_thread(func, *args, **kwargs)

        processor = SessionMessageProcessor(mock_db, run_db=run_db)
        processor.register_session("session-1", str(transcript))
        processor._parsers["session-1"] = MagicMock(parse_lines=MagicMock(return_value=[]))

        await processor._process_session("session-1", str(transcript))

        assert db_calls == [
            "_revive_expired_terminal_session",
            "append_positioned_lines",
        ]

    @pytest.mark.asyncio
    async def test_process_batch_runs_session_updates_on_db_executor(
        self,
        mock_db: MagicMock,
    ) -> None:
        db_calls: list[object] = []

        async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            db_calls.append(func)
            return func(*args, **kwargs)

        session_manager = MagicMock()
        processor = SessionMessageProcessor(
            mock_db,
            session_manager=session_manager,
            run_db=run_db,
        )
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

        with (
            patch.object(processor, "_persist_usage_events", new_callable=AsyncMock),
            patch.object(processor, "_render_and_broadcast_messages", new_callable=AsyncMock),
        ):
            await processor._process_parsed_batch("session-1", [message])

        assert processor._accumulate_stats in db_calls
        assert session_manager.touch in db_calls
        assert session_manager.update_stats in db_calls

    @pytest.mark.asyncio
    async def test_observation_render_runs_on_db_executor(
        self,
        mock_db: MagicMock,
    ) -> None:
        db_calls: list[str] = []

        async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            db_calls.append(func.__name__)
            return func(*args, **kwargs)

        processor = SessionMessageProcessor(mock_db, run_db=run_db)
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

        await processor._render_and_broadcast_messages(
            "session-1",
            [message],
            record_observations=True,
        )

        assert db_calls == ["render_incremental"]

    @pytest.mark.asyncio
    async def test_process_session_advances_past_malformed_timestamp(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
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
    async def test_process_session_with_existing_state(
        self, processor: SessionMessageProcessor, tmp_path: Path
    ) -> None:
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
        self, processor: SessionMessageProcessor, tmp_path: Path
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
        self, processor: SessionMessageProcessor, tmp_path: Path
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
        self, processor: SessionMessageProcessor, tmp_path: Path
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
        with patch.object(
            processor, "_render_and_broadcast_messages", new_callable=AsyncMock
        ) as render_messages:
            await processor._process_session("session-1", str(transcript))

        render_messages.assert_awaited_once_with(
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
        processor: SessionMessageProcessor,
        tmp_path: Path,
        failure_method: str,
        request: pytest.FixtureRequest,
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
        persist_patcher = patch.object(processor, "_persist_usage_events", new_callable=AsyncMock)
        render_patcher = patch.object(
            processor, "_render_and_broadcast_messages", new_callable=AsyncMock
        )
        persist_usage_events: AsyncMock = persist_patcher.start()
        render_messages: AsyncMock = render_patcher.start()
        request.addfinalizer(render_patcher.stop)
        request.addfinalizer(persist_patcher.stop)
        patched_calls = {
            "_persist_usage_events": persist_usage_events,
            "_render_and_broadcast_messages": render_messages,
        }
        failed_call = patched_calls[failure_method]
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
        persisted_batches = persist_usage_events.await_args_list[-2:]
        assert [[message.index for message in call.args[1]] for call in persisted_batches] == [
            [0, 1],
            [2],
        ]
        assert processor._message_indices["session-1"] == 2
        assert processor._stats["session-1"]["message_count"] == 3

    @pytest.mark.asyncio
    async def test_process_session_restores_codex_parser_state_after_batch_failure(
        self,
        processor: SessionMessageProcessor,
        tmp_path: Path,
    ) -> None:
        transcript = tmp_path / "codex-transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "timestamp": "2024-06-15T10:30:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "tool_search_call",
                        "arguments": {"query": "session state"},
                        "status": "completed",
                        "call_id": "call-search",
                        "id": "tsc-1",
                    },
                }
            )
            + "\n"
        )
        processor.register_session("session-1", str(transcript), source="codex")
        parser = processor._parsers["session-1"]
        assert isinstance(parser, CodexTranscriptParser)
        initial_state = parser.snapshot_state()
        process_batch = AsyncMock(
            side_effect=[RuntimeError("mid-batch failure"), {"message_count": 1}]
        )

        with patch.object(processor, "_process_parsed_batch", process_batch):
            with pytest.raises(RuntimeError, match="mid-batch failure"):
                await processor._process_session("session-1", str(transcript))

            assert parser.snapshot_state() == initial_state

            await processor._process_session("session-1", str(transcript))

        assert parser.snapshot_state()["pending_tool_search_use_ids"] == ["call-search"]
        assert process_batch.await_count == 2

    @pytest.mark.asyncio
    async def test_process_session_restores_agy_parser_state_after_batch_failure(
        self,
        processor: SessionMessageProcessor,
        tmp_path: Path,
    ) -> None:
        assert "agy" in PARSER_REGISTRY
        transcript = tmp_path / "transcript_full.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "step_index": 1,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-08-22T08:21:24Z",
                    "content": "run pwd",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-08-22T08:21:24Z",
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pwd"}}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        processor.register_session("session-1", str(transcript), source="agy")
        parser = processor._parsers["session-1"]
        initial_state = parser.snapshot_state()
        process_batch = AsyncMock(
            side_effect=[RuntimeError("mid-batch failure"), {"message_count": 1}]
        )

        with patch.object(processor, "_process_parsed_batch", process_batch):
            with pytest.raises(RuntimeError, match="mid-batch failure"):
                await processor._process_session("session-1", str(transcript))

            assert parser.snapshot_state() == initial_state

            await processor._process_session("session-1", str(transcript))

        assert parser.snapshot_state() != initial_state
        assert process_batch.await_count == 2

    @pytest.mark.asyncio
    async def test_render_failure_does_not_commit_render_state(
        self, processor: SessionMessageProcessor
    ) -> None:
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
        with patch.object(
            processor,
            "_broadcast_rendered_session_message",
            new_callable=AsyncMock,
            side_effect=RuntimeError("broadcast failed"),
        ) as broadcast_message:
            with pytest.raises(RuntimeError, match="broadcast failed"):
                await processor._render_and_broadcast_messages("session-1", [message])

            assert "session-1" not in processor._render_states

            broadcast_message.side_effect = None
            await processor._render_and_broadcast_messages("session-1", [message])

        assert "session-1" in processor._render_states


class TestWebSocketBroadcast:
    """Tests for WebSocket broadcasting functionality."""

    @pytest.mark.asyncio
    async def test_broadcast_messages_to_websocket(
        self, mock_db: MagicMock, tmp_path: Path
    ) -> None:
        """Should broadcast parsed messages to WebSocket server."""
        mock_ws_server = MagicMock()
        mock_ws_server.broadcast = AsyncMock()

        processor = SessionMessageProcessor(mock_db, websocket_server=mock_ws_server)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "hello"}, "timestamp": "2024-01-01T10:00:00Z"}\n'
        )

        processor.register_session("session-1", str(transcript))

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
    async def test_tts_feed_failure_does_not_block_websocket_broadcast(
        self, mock_db: MagicMock, tmp_path: Path
    ) -> None:
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
    async def test_no_broadcast_without_websocket_server(
        self, mock_db: MagicMock, tmp_path: Path
    ) -> None:
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
    async def test_process_multiple_messages_updates_last_index(
        self, mock_db: MagicMock, tmp_path: Path
    ) -> None:
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
    async def test_process_session_captures_model(self, mock_db: MagicMock, tmp_path: Path) -> None:
        """Should extract model from parsed messages and update session."""
        mock_session_manager = MagicMock()
        mock_session_manager.update_model = MagicMock()

        processor = SessionMessageProcessor(mock_db, session_manager=mock_session_manager)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "agent", "message": {"model": "claude-opus-4-5-20251101", "content": [{"type": "text", "text": "hello"}]}, "timestamp": "2024-01-01T10:00:00Z"}\n'
        )

        processor.register_session("session-1", str(transcript))

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
    async def test_live_claude_usage_preserves_one_million_session_model(
        self, mock_db: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeTokenEventStore:
            def __init__(self, _db: object) -> None:
                self.records: list[TokenEvent] = []

            def get_session_totals(self, _session_id: str) -> dict[str, int]:
                return {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                }

            def record(self, event: TokenEvent) -> bool:
                self.records.append(event)
                return True

        store = FakeTokenEventStore(mock_db)
        monkeypatch.setattr("gobby.sessions.processor.TokenEventStore", lambda _db: store)

        session_manager = MagicMock()
        session = MagicMock()
        session.project_id = "proj-1"
        session.source = "claude"
        session.context_window = 200_000
        session.model = "claude-opus-4-8[1m]"
        session_manager.get.return_value = session
        websocket_server = MagicMock()
        websocket_server.broadcast_token_event = AsyncMock()
        websocket_server.broadcast_session_usage_updated = AsyncMock()
        processor = SessionMessageProcessor(
            mock_db,
            websocket_server=websocket_server,
            session_manager=session_manager,
        )
        message = ParsedMessage(
            index=0,
            role="assistant",
            content="done",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={},
            usage=TokenUsage(
                input_tokens=125_071,
                output_tokens=1,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
            model="claude-opus-4-8",
            message_id="message-1",
        )

        await processor._persist_usage_events("session-1", [message])

        event = store.records[0]
        assert event.model == "claude-opus-4-8[1m]"
        assert event.context_window == 1_000_000
        update = session_manager.update_usage.call_args
        assert update.kwargs["model"] == "claude-opus-4-8[1m]"
        assert update.kwargs["context_window"] == 1_000_000
        snapshot = session_manager.update_context_usage.call_args.args[1]
        assert snapshot.context_usage_ratio == pytest.approx(0.125071)
        payload = websocket_server.broadcast_session_usage_updated.await_args.args[0]
        assert payload["model"] == "claude-opus-4-8[1m]"
        assert payload["context_window"] == 1_000_000

    @pytest.mark.asyncio
    async def test_process_session_persists_codex_token_usage(
        self,
        mock_db: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Codex token_count records should update the context pie source fields."""

        class FakeTokenEventStore:
            def __init__(self, _db: object) -> None:
                self.records: list[TokenEvent] = []

            def get_session_totals(self, _session_id: str) -> dict[str, int]:
                return {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                }

            def record(self, event: TokenEvent) -> bool:
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
            model="gpt-5.6-sol",
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
            model="gpt-5.6-sol",
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
    async def test_codex_compaction_occupancy_replaces_pressure_without_token_accounting(
        self,
        mock_db: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class FakeTokenEventStore:
            def __init__(self, _db: object) -> None:
                self.records: list[TokenEvent] = []
                self.totals = {
                    "input_tokens": 222_353,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                }

            def get_session_totals(self, _session_id: str) -> dict[str, int]:
                return dict(self.totals)

            def record(self, event: TokenEvent) -> bool:
                self.records.append(event)
                self.totals["input_tokens"] += event.input_tokens
                self.totals["output_tokens"] += event.output_tokens
                self.totals["cache_creation_tokens"] += event.cache_creation_tokens
                self.totals["cache_read_tokens"] += event.cache_read_tokens
                return True

        store = FakeTokenEventStore(mock_db)
        monkeypatch.setattr(
            "gobby.sessions.processor.TokenEventStore",
            lambda _db: store,
        )

        session_manager = MagicMock()
        session = MagicMock()
        session.project_id = "proj-1"
        session.source = "codex"
        session.context_window = 258_400
        session.model = "gpt-5.6-sol"
        session.context_used_tokens = 222_353
        session.context_usage_ratio = 222_353 / 258_400
        session_manager.get.return_value = session

        def persist_context(_session_id: str, snapshot: Any) -> bool:
            session.context_used_tokens = snapshot.context_used_tokens
            session.context_usage_ratio = snapshot.context_usage_ratio
            return True

        session_manager.update_context_usage.side_effect = persist_context
        websocket_server = MagicMock()
        websocket_server.broadcast_token_event = AsyncMock()
        websocket_server.broadcast_session_usage_updated = AsyncMock()
        processor = SessionMessageProcessor(
            mock_db,
            websocket_server=websocket_server,
            session_manager=session_manager,
        )
        transcript = tmp_path / "codex-compaction.jsonl"

        def token_count_line(last_token_usage: dict[str, int]) -> str:
            return json.dumps(
                {
                    "timestamp": "2026-07-25T12:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": last_token_usage,
                            "model_context_window": 258_400,
                        },
                    },
                }
            )

        transcript.write_text(
            token_count_line(
                {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 7_248,
                }
            )
            + "\n"
        )
        processor._active_sessions["session-1"] = str(transcript)
        processor._parsers["session-1"] = CodexTranscriptParser()

        await processor._process_session("session-1", str(transcript))

        assert session.context_used_tokens == 7_248
        assert session.context_usage_ratio == pytest.approx(7_248 / 258_400)
        assert store.records == []
        session_manager.update_usage.assert_not_called()
        websocket_server.broadcast_token_event.assert_not_awaited()

        with transcript.open("a") as transcript_file:
            transcript_file.write(
                token_count_line(
                    {
                        "input_tokens": 8_000,
                        "cached_input_tokens": 7_000,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 10,
                        "total_tokens": 8_050,
                    }
                )
                + "\n"
            )

        await processor._process_session("session-1", str(transcript))

        assert len(store.records) == 1
        assert session.context_used_tokens == 8_000
        assert session.context_usage_ratio == pytest.approx(8_000 / 258_400)
        session_manager.update_usage.assert_called_once()
        websocket_server.broadcast_token_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_token_usage_refreshes_session_without_token_broadcast(
        self, mock_db: MagicMock, monkeypatch: pytest.MonkeyPatch
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

            def record(self, _event: TokenEvent) -> bool:
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
            model="gpt-5.6-sol",
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
            model="gpt-5.6-sol",
        )
        mock_session_manager.update_context_usage.assert_called_once()
        usage_payload = mock_ws.broadcast_session_usage_updated.await_args.args[0]
        assert usage_payload["usage_input_tokens"] == 11392
        assert usage_payload["context_used_tokens"] == 104960
        mock_ws.broadcast_token_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_usage_events_ignores_session_lookup_db_errors(
        self,
        mock_db: MagicMock,
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
    async def test_persist_usage_events_runs_storage_calls_on_db_executor(
        self,
        mock_db: MagicMock,
    ) -> None:
        class FakeTokenEventStore:
            def get_session_totals(self, _session_id: str) -> dict[str, int]:
                return {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                }

            def record(self, _event: TokenEvent) -> bool:
                return True

        db_calls: list[object] = []

        async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            db_calls.append(func)
            return func(*args, **kwargs)

        session_manager = MagicMock()
        session_manager.get.return_value = MagicMock(
            project_id="project-1",
            source="codex",
            context_window=128_000,
            model="gpt-5",
        )
        store = FakeTokenEventStore()
        processor = SessionMessageProcessor(
            mock_db,
            session_manager=session_manager,
            run_db=run_db,
        )
        message = ParsedMessage(
            index=0,
            role="assistant",
            content="done",
            content_type="text",
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=datetime.now(),
            raw_json={},
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            model="gpt-5",
        )

        with patch.object(processor, "_new_token_event_store", return_value=store):
            await processor._persist_usage_events("session-1", [message])

        assert session_manager.get in db_calls
        assert store.get_session_totals in db_calls
        assert store.record in db_calls
        assert session_manager.update_usage in db_calls
        assert session_manager.update_context_usage in db_calls

    @pytest.mark.asyncio
    async def test_process_session_records_grok_window_only_snapshot(
        self,
        mock_db: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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

            def record(self, _event: TokenEvent) -> bool:
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
        mock_db: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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

            def record(self, _event: TokenEvent) -> bool:
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
    async def test_process_session_skips_model_update_when_none(
        self, mock_db: MagicMock, tmp_path: Path
    ) -> None:
        """Should not update model when parsed message has no model."""
        mock_session_manager = MagicMock()
        mock_session_manager.update_model = MagicMock()

        processor = SessionMessageProcessor(mock_db, session_manager=mock_session_manager)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"content": "hello"}, "timestamp": "2024-01-01T10:00:00Z"}\n'
        )

        processor.register_session("session-1", str(transcript))

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

    def test_default_poll_interval(self, mock_db: MagicMock) -> None:
        """Should use default poll interval of 2.0 seconds."""
        processor = SessionMessageProcessor(mock_db)
        assert processor.poll_interval == 2.0

    def test_custom_poll_interval(self, mock_db: MagicMock) -> None:
        """Should accept custom poll interval."""
        processor = SessionMessageProcessor(mock_db, poll_interval=5.0)
        assert processor.poll_interval == 5.0

    def test_initial_state(self, mock_db: MagicMock) -> None:
        """Should initialize with empty state."""
        processor = SessionMessageProcessor(mock_db)
        assert processor._active_sessions == {}
        assert processor._parsers == {}
        assert processor._running is False
        assert processor._task is None

    def test_websocket_server_optional(self, mock_db: MagicMock) -> None:
        """Should accept optional WebSocket server."""
        mock_ws = MagicMock()
        processor = SessionMessageProcessor(mock_db, websocket_server=mock_ws)
        assert processor.websocket_server is mock_ws

        processor_no_ws = SessionMessageProcessor(mock_db)
        assert processor_no_ws.websocket_server is None


def _codex_event_msg(payload_type: str, **payload_extra: Any) -> str:
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
        self, mock_db: MagicMock, tmp_path: Path
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


class TestFilterSessionTitleMessages:
    """Provider-native title records are metadata-only transcript entries."""

    @staticmethod
    def _title_message(content: str, index: int = 0) -> ParsedMessage:
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
        )

    @staticmethod
    def _text_message(content: str, index: int = 1) -> ParsedMessage:
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

    def test_filters_title_metadata_without_persisting_it(self, mock_db: MagicMock) -> None:
        processor = SessionMessageProcessor(mock_db)
        session_manager = MagicMock()
        processor.session_manager = session_manager
        text_message = self._text_message("Hello world")

        result = processor._filter_session_title_messages(
            [
                self._title_message("17903 └── Mechanical prompt title"),
                text_message,
                self._title_message("Provider replacement title", index=2),
            ],
        )

        assert result == [text_message]
        session_manager.get.assert_not_called()
        session_manager.update_title.assert_not_called()

    def test_returns_non_title_messages_unchanged(self, mock_db: MagicMock) -> None:
        processor = SessionMessageProcessor(mock_db)
        messages = [self._text_message("Hello", index=0), self._text_message("World", index=1)]

        assert processor._filter_session_title_messages(messages) == messages
        assert processor._filter_session_title_messages([]) == []

    @pytest.mark.asyncio
    async def test_byte_offset_prevents_reprocessing_mcp_lifecycle_lines(
        self, mock_db: MagicMock, tmp_path: Path
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
        self, mock_db: MagicMock, tmp_path: Path
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
    async def test_mcp_end_error_does_not_dispatch_workflow_hooks(
        self, mock_db: MagicMock, tmp_path: Path
    ) -> None:
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
