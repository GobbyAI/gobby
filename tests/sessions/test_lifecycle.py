import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.storage.session_models import Session

pytestmark = pytest.mark.unit

_SESSION_MANAGER_PATCH = "gobby.sessions.lifecycle.SessionManager"
DROID_FIXTURE_DIR = Path(__file__).parent / "transcripts" / "fixtures" / "droid"
DROID_FIXTURE_JSONL = DROID_FIXTURE_DIR / "dbf95187-5fa4-43a0-b207-8c24f412baf7.jsonl"
DROID_FIXTURE_SETTINGS = DROID_FIXTURE_DIR / "dbf95187-5fa4-43a0-b207-8c24f412baf7.settings.json"


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def mock_config():
    config = MagicMock(spec=SessionLifecycleConfig)
    config.expire_check_interval_minutes = 1
    config.transcript_processing_interval_minutes = 1
    config.active_session_pause_minutes = 30
    config.stale_session_timeout_hours = 24
    config.transcript_processing_batch_size = 10
    return config


@pytest.fixture
def manager(mock_db, mock_config):
    with patch(_SESSION_MANAGER_PATCH):
        return SessionLifecycleManager(mock_db, mock_config)


class TestSessionLifecycleManager:
    """Tests for SessionLifecycleManager."""

    @pytest.mark.asyncio
    async def test_start_creates_background_tasks(self, manager):
        """Test that start() creates background tasks."""
        await manager.start()

        assert manager._running is True
        assert manager._expire_task is not None
        assert manager._process_task is not None
        assert not manager._expire_task.done()
        assert not manager._process_task.done()

        # Clean stop
        await manager.stop()
        assert manager._running is False
        assert manager._expire_task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_background_tasks(self, manager):
        """Test that stop() cancels tasks."""
        await manager.start()

        expire_task = manager._expire_task
        process_task = manager._process_task

        await manager.stop()

        assert expire_task.cancelled() or expire_task.done()
        assert process_task.cancelled() or process_task.done()

    @pytest.mark.asyncio
    async def test_expire_stale_sessions(self, manager):
        """Test expiring stale sessions."""
        # Setup mocks
        manager.session_manager.pause_inactive_active_sessions.return_value = 2
        manager.session_manager.expire_orphaned_handoff_sessions.return_value = 1
        manager.session_manager.expire_stale_sessions.return_value = 3
        manager.session_manager.expire_empty_sessions.return_value = 4
        manager.session_manager.prune_empty_sessions.return_value = 5

        count = await manager._expire_stale_sessions()

        assert count == 15
        manager.session_manager.pause_inactive_active_sessions.assert_called_once_with(
            timeout_minutes=manager.config.active_session_pause_minutes
        )
        manager.session_manager.expire_orphaned_handoff_sessions.assert_called_once_with(
            timeout_minutes=30
        )
        manager.session_manager.expire_stale_sessions.assert_called_once_with(
            timeout_hours=manager.config.stale_session_timeout_hours
        )
        manager.session_manager.expire_empty_sessions.assert_called_once_with(timeout_hours=2)
        manager.session_manager.prune_empty_sessions.assert_called_once_with(min_age_hours=1)

    @pytest.mark.asyncio
    async def test_process_pending_transcripts_none_found(self, manager):
        """Test processing when no sessions pending."""
        manager.session_manager.get_pending_transcript_sessions.return_value = []

        processed = await manager._process_pending_transcripts()

        assert processed == 0
        manager.session_manager.mark_transcript_processed.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_pending_transcripts_success(self, tmp_path, manager):
        """Test successful processing of a transcript."""
        # Create a mock session
        session = MagicMock(spec=Session)
        session.id = "s1"
        session.transcript_path = str(tmp_path / "transcript.jsonl")
        session.external_id = "ext-s1"
        session.agent_depth = 0
        session.source = "claude"

        manager.session_manager.get_pending_transcript_sessions.return_value = [session]

        # manager.session_manager.get() must return a session with summary_markdown
        # so that mark_transcript_processed is called (gated on summary presence)
        refreshed = MagicMock()
        refreshed.summary_markdown = "summary content"
        manager.session_manager.get.return_value = refreshed

        # Create real file content
        with open(session.transcript_path, "w") as f:
            f.write('{"type": "message", "content": "hello"}\n')

        # Mock _process_session_transcript to avoid complex parsing logic
        with patch.object(
            manager, "_process_session_transcript", new_callable=AsyncMock
        ) as mock_process:
            processed = await manager._process_pending_transcripts()

            assert processed == 1
            mock_process.assert_awaited_once_with("s1", session.transcript_path)
            manager.session_manager.mark_transcript_processed.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_process_pending_transcripts_skips_subagent_sessions(self, tmp_path, manager):
        """Subagent sessions (agent_depth > 0) skip memory extraction and summary generation."""
        session = MagicMock(spec=Session)
        session.id = "s-sub"
        session.transcript_path = str(tmp_path / "transcript.jsonl")
        session.external_id = "ext-sub"
        session.agent_depth = 1
        session.source = "claude"

        manager.session_manager.get_pending_transcript_sessions.return_value = [session]

        with open(session.transcript_path, "w") as f:
            f.write('{"type": "message", "content": "hello"}\n')

        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager, "_generate_summaries_if_needed", new_callable=AsyncMock
            ) as mock_sum,
        ):
            processed = await manager._process_pending_transcripts()

            assert processed == 1
            mock_sum.assert_not_awaited()
            manager.session_manager.mark_transcript_processed.assert_called_once_with("s-sub")

    @pytest.mark.asyncio
    async def test_process_pending_transcripts_skips_pipeline_sessions(self, tmp_path, manager):
        """Pipeline sessions skip summary generation."""
        session = MagicMock(spec=Session)
        session.id = "s-pipe"
        session.transcript_path = str(tmp_path / "transcript.jsonl")
        session.external_id = "ext-pipe"
        session.agent_depth = 0
        session.source = "pipeline"

        manager.session_manager.get_pending_transcript_sessions.return_value = [session]

        with open(session.transcript_path, "w") as f:
            f.write('{"type": "message", "content": "hello"}\n')

        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager, "_generate_summaries_if_needed", new_callable=AsyncMock
            ) as mock_sum,
        ):
            processed = await manager._process_pending_transcripts()

            assert processed == 1
            mock_sum.assert_not_awaited()
            manager.session_manager.mark_transcript_processed.assert_called_once_with("s-pipe")

    @pytest.mark.asyncio
    async def test_process_session_transcript_real_parsing(self, tmp_path, manager):
        """Test parsing logic inside _process_session_transcript."""
        transcript_path = tmp_path / "transcript.jsonl"
        with open(transcript_path, "w") as f:
            f.write(
                '{"type": "message", "message": {"content": "hello"}, "timestamp": "2024-01-01T00:00:00Z"}\n'
            )

        message_mock = MagicMock()
        message_mock.index = 5

        with patch("gobby.sessions.lifecycle.ClaudeTranscriptParser") as MockParser:
            parser_instance = MockParser.return_value
            parser_instance.parse_lines.return_value = [message_mock]

            await manager._process_session_transcript("s1", str(transcript_path))

            parser_instance.parse_lines.assert_called_once()
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_process_session_transcript_missing_file(self, manager):
        """Test handling of missing file."""
        await manager._process_session_transcript("s1", "/non/existent/file.jsonl")
        assert manager.session_manager.get.call_count == 0

    @pytest.mark.asyncio
    async def test_process_session_transcript_read_error(self, tmp_path, manager):
        """Test error reading transcript file."""
        transcript_path = tmp_path / "transcript.jsonl"
        with open(transcript_path, "w") as f:
            f.write("content")

        # Permission error mock or similar
        with patch("builtins.open", side_effect=OSError("Read error")):
            with pytest.raises(IOError):
                await manager._process_session_transcript("s1", str(transcript_path))

    @pytest.mark.asyncio
    async def test_process_session_transcript_empty_file(self, tmp_path, manager):
        """Test processing empty file."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.touch()

        await manager._process_session_transcript("s1", str(transcript_path))
        assert manager.session_manager.get.call_count == 0

    @pytest.mark.asyncio
    async def test_process_session_transcript_no_messages(self, tmp_path, manager):
        """Test file with no valid messages."""
        transcript_path = tmp_path / "transcript.jsonl"
        with open(transcript_path, "w") as f:
            f.write('{"type": "unknown"}\n')

        with patch("gobby.sessions.lifecycle.ClaudeTranscriptParser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []

            await manager._process_session_transcript("s1", str(transcript_path))
        assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_process_pending_transcripts_loop_error(self, manager):
        """Test error handling in process loop (single iteration logic)."""
        manager.session_manager.get_pending_transcript_sessions.side_effect = Exception("DB Error")

        # Should propagate or handle? _process_pending_transcripts does NOT catch its own top-level errors (the loop does)
        with pytest.raises(Exception, match="DB Error"):
            await manager._process_pending_transcripts()

    @pytest.mark.asyncio
    async def test_process_pending_transcripts_individual_error(self, manager):
        """Test error handling for individual session processing.

        Even when transcript processing fails for a session, summary/memory
        extraction still runs.  mark_transcript_processed is gated on
        summary_markdown presence: sessions with summaries are marked done,
        those without are deferred for retry.
        """
        _digest = "### Turn 1\nA\n### Turn 2\nB\n### Turn 3\nC"
        s1 = MagicMock(id="s1", agent_depth=0, source="claude", digest_markdown=_digest)
        s2 = MagicMock(id="s2", agent_depth=0, source="claude", digest_markdown=_digest)
        manager.session_manager.get_pending_transcript_sessions.return_value = [s1, s2]

        # Enable llm_service so the summary-gating logic activates
        manager.llm_service = MagicMock()

        # s1 has no summary (will be deferred), s2 has summary (will be processed)
        s1_refreshed = MagicMock()
        s1_refreshed.summary_markdown = None
        s2_refreshed = MagicMock()
        s2_refreshed.summary_markdown = "summary content"
        manager.session_manager.get.side_effect = [s1_refreshed, s2_refreshed]

        # Mock helper methods to isolate loop logic
        with (
            patch.object(
                manager, "_process_session_transcript", new_callable=AsyncMock
            ) as mock_proc,
            patch.object(manager, "_generate_summaries_if_needed", new_callable=AsyncMock),
        ):
            mock_proc.side_effect = [Exception("Fail"), None]

            processed = await manager._process_pending_transcripts()

            # s1 deferred (no summary), s2 processed (has summary)
            assert processed == 1
            assert mock_proc.call_count == 2

    @pytest.mark.asyncio
    async def test_pending_graph_memory_db_work_uses_memory_run_db(self, mock_db, mock_config):
        """Queued graph memory DB work uses the bounded memory DB executor."""

        class MemoryManagerStub:
            def __init__(self) -> None:
                self.kg_service = AsyncMock()
                self.kg_service.add_to_graph.return_value = SimpleNamespace(status="success")
                self.pending = [
                    SimpleNamespace(id="mem-1", content="remember this", project_id="proj-1")
                ]
                self.marked: list[str] = []
                self.run_db_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

            async def run_db(self, func, *args, **kwargs):
                self.run_db_calls.append((func, args, kwargs))
                return func(*args, **kwargs)

            def get_pending_graph_memories(self, limit: int = 20):
                assert limit == 3
                return self.pending

            def mark_graph_processed(self, memory_id: str) -> None:
                self.marked.append(memory_id)

        memory_manager = MemoryManagerStub()
        with patch(_SESSION_MANAGER_PATCH):
            manager = SessionLifecycleManager(mock_db, mock_config, memory_manager=memory_manager)

        with patch(
            "gobby.sessions.lifecycle.asyncio.to_thread", new_callable=AsyncMock
        ) as to_thread:
            processed = await manager._process_pending_graph_memories(batch_size=3)

        assert processed == 1
        assert memory_manager.marked == ["mem-1"]
        assert [call[0] for call in memory_manager.run_db_calls] == [
            memory_manager.get_pending_graph_memories,
            memory_manager.mark_graph_processed,
        ]
        to_thread.assert_not_awaited()


class TestBackgroundLoops:
    """Tests for infinite background loops."""

    @pytest.mark.asyncio
    async def test_expire_loop_runs_and_calls_delegate(self, manager):
        """Test expire loop calls delegate and sleeps."""
        manager._running = True

        # Mock delegate to verify call
        manager._expire_stale_sessions = AsyncMock(return_value=0)

        # Mock sleep to run once then stop loop
        async def side_effect_sleep(seconds):
            manager._running = False  # Stop after first sleep
            return

        with patch("asyncio.sleep", side_effect=side_effect_sleep) as mock_sleep:
            await manager._expire_loop()

            manager._expire_stale_sessions.assert_awaited_once()
            mock_sleep.assert_awaited_once_with(manager.config.expire_check_interval_minutes * 60)
            assert manager._running is False

    @pytest.mark.asyncio
    async def test_process_loop_runs_and_calls_delegate(self, manager):
        """Test process loop calls delegate and sleeps."""
        manager._running = True

        manager._process_pending_transcripts = AsyncMock(return_value=0)

        async def side_effect_sleep(seconds):
            manager._running = False
            return

        with patch("asyncio.sleep", side_effect=side_effect_sleep) as mock_sleep:
            await manager._process_loop()

            manager._process_pending_transcripts.assert_awaited_once()
            mock_sleep.assert_awaited_once_with(
                manager.config.transcript_processing_interval_minutes * 60
            )
            assert manager._running is False

    @pytest.mark.asyncio
    async def test_loops_handle_exceptions(self, manager):
        """Test loops catch exceptions from delegate."""
        manager._running = True

        # Delegate raises exception
        manager._expire_stale_sessions = AsyncMock(side_effect=Exception("Boom"))

        # Log error is called
        with patch("gobby.sessions.lifecycle.logger.error") as mock_logger:

            async def side_effect_sleep(seconds):
                manager._running = False
                return

            with patch("asyncio.sleep", side_effect=side_effect_sleep):
                await manager._expire_loop()

            mock_logger.assert_called_with("Error in expire loop: Boom")
            assert manager._running is False

    @pytest.mark.asyncio
    async def test_loops_handle_cancellation(self, manager):
        """Test loops exit on CancelledError during sleep."""
        manager._running = True
        manager._expire_stale_sessions = AsyncMock()

        with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            await manager._expire_loop()
            assert manager._expire_stale_sessions.await_count == 1


class TestPromptFileCleanup:
    """Tests for _cleanup_prompt_files (#7389)."""

    def test_removes_old_prompt_files(self, tmp_path, manager):
        """Old prompt files are deleted."""
        prompt_dir = tmp_path / "gobby-prompts"
        prompt_dir.mkdir()

        # Create a file and backdate its mtime by 2 hours
        old_file = prompt_dir / "prompt-old-session.txt"
        old_file.write_text("old prompt")
        old_mtime = time.time() - 7200
        os.utime(old_file, (old_mtime, old_mtime))

        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            removed = manager._cleanup_prompt_files(max_age_seconds=3600)

        assert removed == 1
        assert not old_file.exists()

    def test_keeps_recent_prompt_files(self, tmp_path, manager):
        """Recent prompt files are kept."""
        prompt_dir = tmp_path / "gobby-prompts"
        prompt_dir.mkdir()

        recent_file = prompt_dir / "prompt-recent-session.txt"
        recent_file.write_text("recent prompt")
        # File just created — mtime is now

        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            removed = manager._cleanup_prompt_files(max_age_seconds=3600)

        assert removed == 0
        assert recent_file.exists()

    def test_mixed_old_and_recent(self, tmp_path, manager):
        """Only old files are removed, recent ones kept."""
        prompt_dir = tmp_path / "gobby-prompts"
        prompt_dir.mkdir()

        old_file = prompt_dir / "prompt-old.txt"
        old_file.write_text("old")
        old_mtime = time.time() - 7200
        os.utime(old_file, (old_mtime, old_mtime))

        recent_file = prompt_dir / "prompt-recent.txt"
        recent_file.write_text("recent")

        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            removed = manager._cleanup_prompt_files(max_age_seconds=3600)

        assert removed == 1
        assert not old_file.exists()
        assert recent_file.exists()

    def test_no_prompt_dir(self, tmp_path, manager):
        """Returns 0 when prompt directory doesn't exist."""
        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            removed = manager._cleanup_prompt_files()

        assert removed == 0

    @pytest.mark.asyncio
    async def test_expire_calls_cleanup(self, manager):
        """_expire_stale_sessions calls _cleanup_prompt_files."""
        manager.session_manager = MagicMock()
        manager.session_manager.pause_inactive_active_sessions.return_value = 0
        manager.session_manager.expire_orphaned_handoff_sessions.return_value = 0
        manager.session_manager.expire_stale_sessions.return_value = 0
        manager.session_manager.expire_empty_sessions.return_value = 0
        manager.session_manager.prune_empty_sessions.return_value = 0

        with patch.object(manager, "_cleanup_prompt_files") as mock_cleanup:
            expired_count = await manager._expire_stale_sessions()

        assert expired_count == 0
        mock_cleanup.assert_called_once()


class TestGenerateSummariesIfNeeded:
    """Tests for _generate_summaries_if_needed."""

    @pytest.mark.asyncio
    async def test_no_llm_service(self, manager):
        """Skips when llm_service is None."""
        manager.llm_service = None
        await manager._generate_summaries_if_needed("sess-1")
        manager.session_manager.get.assert_not_called()
        assert manager.session_manager.get.call_count == 0

    @pytest.mark.asyncio
    async def test_session_not_found(self, manager):
        """Skips when session not found."""
        manager.llm_service = MagicMock()
        manager.session_manager.get.return_value = None
        await manager._generate_summaries_if_needed("sess-1")
        assert manager.session_manager.get.call_args.args == ("sess-1",)

    @pytest.mark.asyncio
    async def test_session_already_has_summary(self, manager):
        """Skips when session already has summary."""
        manager.llm_service = MagicMock()
        session = MagicMock()
        session.summary_markdown = "existing summary"
        manager.session_manager.get.return_value = session
        await manager._generate_summaries_if_needed("sess-1")
        assert manager.session_manager.get.call_args.args == ("sess-1",)

    @pytest.mark.asyncio
    async def test_sentinel_summary_does_not_count_as_existing_summary(self, manager):
        """Provider failure sentinels are retried instead of treated as summaries."""
        manager.llm_service = MagicMock()
        session = MagicMock()
        session.summary_markdown = "Session summary generation failed: provider unavailable"
        session.digest_markdown = "### Turn 1\nDigest source"
        session.transcript_path = None
        manager.session_manager.get.return_value = session

        with patch(
            "gobby.sessions.summarize.generate_session_summaries",
            new_callable=AsyncMock,
        ) as mock_gen:
            await manager._generate_summaries_if_needed("sess-1")

        mock_gen.assert_awaited_once()
        assert mock_gen.await_args.kwargs["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_session_no_transcript_path(self, manager):
        """Skips when session has no transcript_path."""
        manager.llm_service = MagicMock()
        session = MagicMock()
        session.summary_markdown = None
        session.transcript_path = None
        manager.session_manager.get.return_value = session
        await manager._generate_summaries_if_needed("sess-1")
        assert manager.session_manager.get.call_args.args == ("sess-1",)

    @pytest.mark.asyncio
    async def test_summary_generation_exception(self, manager):
        """Catches summary generation errors."""
        manager.llm_service = MagicMock()
        session = MagicMock()
        session.summary_markdown = None
        session.transcript_path = "/path/to/transcript.jsonl"
        manager.session_manager.get.return_value = session

        with patch(
            "gobby.sessions.summarize.generate_session_summaries",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Summary error"),
        ):
            # Should not raise
            await manager._generate_summaries_if_needed("sess-1")
        assert manager.session_manager.get.call_args.args == ("sess-1",)

    @pytest.mark.asyncio
    async def test_summary_generation_success(self, manager):
        """Successful summary generation."""
        manager.llm_service = MagicMock()
        session = MagicMock()
        session.summary_markdown = None
        session.transcript_path = "/path/to/transcript.jsonl"
        manager.session_manager.get.return_value = session

        with patch(
            "gobby.sessions.summarize.generate_session_summaries",
            new_callable=AsyncMock,
        ) as mock_gen:
            await manager._generate_summaries_if_needed("sess-1")
            mock_gen.assert_awaited_once()
            assert mock_gen.await_args.kwargs["session_id"] == "sess-1"


class TestPurgeSoftDeletedDefinitions:
    """Tests for _purge_soft_deleted_definitions."""

    @pytest.mark.asyncio
    async def test_success(self, manager):
        """Purge runs without error."""
        with patch("gobby.storage.workflow_definitions.LocalWorkflowDefinitionManager") as MockWFM:
            await manager._purge_soft_deleted_definitions()
            MockWFM.return_value.purge_deleted.assert_called_once_with(older_than_days=30)
        assert MockWFM.return_value.purge_deleted.call_count == 1

    @pytest.mark.asyncio
    async def test_exception_handled(self, manager):
        """Purge errors are caught and logged."""
        with patch("gobby.storage.workflow_definitions.LocalWorkflowDefinitionManager") as MockWFM:
            MockWFM.return_value.purge_deleted.side_effect = Exception("DB error")
            # Should not raise
            await manager._purge_soft_deleted_definitions()
        assert MockWFM.return_value.purge_deleted.call_count == 1


class TestProcessSessionTranscriptParsers:
    """Tests for _process_session_transcript parser selection."""

    @pytest.mark.asyncio
    async def test_gemini_parser_selected(self, tmp_path, manager):
        """Gemini source uses GeminiTranscriptParser."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "gemini"
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.lifecycle.GeminiTranscriptParser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []
            await manager._process_session_transcript("s1", str(transcript_path))
            MockParser.assert_called_once()
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_codex_parser_selected(self, tmp_path, manager):
        """Codex source uses CodexTranscriptParser."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "codex"
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.lifecycle.CodexTranscriptParser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []
            await manager._process_session_transcript("s1", str(transcript_path))
            MockParser.assert_called_once()
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_droid_parser_selected_with_transcript_path(self, tmp_path, manager):
        """Droid source uses DroidTranscriptParser with the transcript path for sidecars."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "droid"
        session.transcript_path = str(transcript_path)
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.lifecycle.DroidTranscriptParser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []
            await manager._process_session_transcript("s1", str(transcript_path))
            MockParser.assert_called_once_with(
                session_id="s1",
                transcript_path=str(transcript_path),
            )
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_droid_backfill_records_sidecar_token_usage(self, tmp_path, manager):
        """Droid lifecycle backfill records TokenUsage from the adjacent settings sidecar."""
        transcript_path = tmp_path / "droid-session.jsonl"
        transcript_path.write_text(DROID_FIXTURE_JSONL.read_text(encoding="utf-8"))
        transcript_path.with_suffix(".settings.json").write_text(
            DROID_FIXTURE_SETTINGS.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        session = MagicMock()
        session.source = "droid"
        session.transcript_path = str(transcript_path)
        session.project_id = "project-id"
        session.context_window = None
        session.model = None
        manager.session_manager.get.return_value = session

        zero_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }
        manager.token_event_store = MagicMock()
        manager.token_event_store.get_session_totals.side_effect = [
            dict(zero_totals),
            dict(zero_totals),
        ]
        manager.token_event_store.record.return_value = True

        await manager._process_session_transcript("s1", str(transcript_path))

        event = manager.token_event_store.record.call_args.args[0]
        assert event.session_id == "s1"
        assert event.project_id == "project-id"
        assert event.source == "droid"
        assert event.origin == "transcript"
        assert event.model == "claude-3-7-sonnet-latest"
        assert event.input_tokens == 22571
        assert event.output_tokens == 384
        assert event.cache_creation_tokens == 0
        assert event.cache_read_tokens == 26112
        assert event.metadata == {"content_type": "tool_use"}
        manager.session_manager.update_usage.assert_called_once_with(
            session_id="s1",
            input_tokens=22571,
            output_tokens=384,
            cache_creation_tokens=0,
            cache_read_tokens=26112,
            context_window=None,
            model="claude-3-7-sonnet-latest",
        )

    @pytest.mark.asyncio
    async def test_session_not_found_returns_early(self, tmp_path, manager):
        """Returns early when session not found in DB."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        manager.session_manager.get.return_value = None
        await manager._process_session_transcript("s1", str(transcript_path))
        assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_none_transcript_path(self, manager):
        """Handles None transcript_path."""
        await manager._process_session_transcript("s1", None)
        assert manager.session_manager.get.call_count == 0


class TestProcessSessionTranscriptJsonDispatch:
    """Tests for .json file dispatch to parse_session_json."""

    @pytest.mark.asyncio
    async def test_gemini_json_uses_parse_session_json(self, tmp_path, manager):
        """Gemini .json transcript dispatches to parse_session_json, not parse_lines."""
        import json

        transcript_path = tmp_path / "session-abc.json"
        transcript_path.write_text(
            json.dumps(
                {"sessionId": "abc", "messages": [{"type": "user", "content": [{"text": "hi"}]}]}
            )
        )

        session = MagicMock()
        session.source = "gemini"
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.lifecycle.GeminiTranscriptParser") as MockParser:
            MockParser.return_value.parse_session_json.return_value = []
            # Ensure hasattr check passes
            MockParser.return_value.parse_session_json.__name__ = "parse_session_json"
            await manager._process_session_transcript("s1", str(transcript_path))
            MockParser.return_value.parse_session_json.assert_called_once()
            MockParser.return_value.parse_lines.assert_not_called()
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_jsonl_still_uses_parse_lines(self, tmp_path, manager):
        """JSONL transcripts still use parse_lines even for Gemini."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "gemini"
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.lifecycle.GeminiTranscriptParser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []
            await manager._process_session_transcript("s1", str(transcript_path))
            MockParser.return_value.parse_lines.assert_called_once()
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_invalid_json_returns_early(self, tmp_path, manager):
        """Invalid JSON in .json file logs error and returns without crashing."""
        transcript_path = tmp_path / "session-bad.json"
        transcript_path.write_text("{invalid json content")

        session = MagicMock()
        session.source = "gemini"
        manager.session_manager.get.return_value = session

        # Should not raise
        await manager._process_session_transcript("s1", str(transcript_path))
        manager.session_manager.update_usage.assert_not_called()
        assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_json_messages_aggregate_tokens(self, tmp_path, manager):
        """Token usage from parse_session_json messages is aggregated and saved."""
        import json

        from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage

        transcript_path = tmp_path / "session-tok.json"
        transcript_path.write_text(json.dumps({"sessionId": "tok", "messages": []}))

        session = MagicMock()
        session.source = "gemini"
        manager.session_manager.get.return_value = session

        msg = MagicMock(spec=ParsedMessage)
        msg.model = "gemini-2.5-pro"
        msg.usage = TokenUsage(input_tokens=100, output_tokens=50)

        with patch("gobby.sessions.lifecycle.GeminiTranscriptParser") as MockParser:
            MockParser.return_value.parse_session_json.return_value = [msg]
            MockParser.return_value.parse_session_json.__name__ = "parse_session_json"
            await manager._process_session_transcript("s1", str(transcript_path))

        manager.session_manager.update_usage.assert_called_once()
        call_kwargs = manager.session_manager.update_usage.call_args
        assert call_kwargs.kwargs["input_tokens"] == 100
        assert call_kwargs.kwargs["output_tokens"] == 50


class TestProcessSessionTranscriptTokenPreservation:
    """Tests for preserving hook-captured tokens when transcript yields 0."""

    @pytest.mark.asyncio
    async def test_zero_tokens_preserves_existing_nonzero(self, tmp_path, manager):
        """When transcript yields 0 tokens but session has existing tokens, don't overwrite."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        session.usage_input_tokens = 5000
        session.usage_output_tokens = 2000

        # First get() returns session for parser selection, second for preservation check
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.lifecycle.ClaudeTranscriptParser") as MockParser:
            # Parser returns messages with no usage
            msg = MagicMock()
            msg.model = None
            msg.usage = None
            MockParser.return_value.parse_lines.return_value = [msg]
            await manager._process_session_transcript("s1", str(transcript_path))

        # update_usage should NOT be called — preserving existing values
        manager.session_manager.update_usage.assert_not_called()
        assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_zero_tokens_updates_when_existing_also_zero(self, tmp_path, manager):
        """When both transcript and existing are 0, update_usage is still called (nothing to preserve)."""
        from gobby.sessions.transcripts.base import ParsedMessage

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        session.usage_input_tokens = 0
        session.usage_output_tokens = 0

        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.lifecycle.ClaudeTranscriptParser") as MockParser:
            # spec=ParsedMessage so the lifecycle path's ParsedToolEvent filter
            # doesn't drop the mock.
            msg = MagicMock(spec=ParsedMessage)
            msg.model = None
            msg.usage = None
            MockParser.return_value.parse_lines.return_value = [msg]
            await manager._process_session_transcript("s1", str(transcript_path))

        # Both are 0, so no preservation needed — update proceeds (harmless write of zeros)
        manager.session_manager.update_usage.assert_called_once()
        call_kwargs = manager.session_manager.update_usage.call_args.kwargs
        assert call_kwargs["input_tokens"] == 0
        assert call_kwargs["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_nonzero_tokens_always_updates(self, tmp_path, manager):
        """When transcript yields real tokens, always update regardless of existing values."""
        from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        session.usage_input_tokens = 5000
        session.usage_output_tokens = 2000
        manager.session_manager.get.return_value = session

        msg = MagicMock(spec=ParsedMessage)
        msg.model = "claude-sonnet-4-6"
        msg.usage = TokenUsage(input_tokens=8000, output_tokens=3000)

        with patch("gobby.sessions.lifecycle.ClaudeTranscriptParser") as MockParser:
            MockParser.return_value.parse_lines.return_value = [msg]
            await manager._process_session_transcript("s1", str(transcript_path))

        manager.session_manager.update_usage.assert_called_once()
        call_kwargs = manager.session_manager.update_usage.call_args
        assert call_kwargs.kwargs["input_tokens"] == 8000
        assert call_kwargs.kwargs["output_tokens"] == 3000


class TestProcessPendingTranscriptsArchive:
    """Tests for transcript archive and message purge logic."""

    @pytest.mark.asyncio
    async def test_archive_success_purges_messages(self, manager):
        """Successful archive triggers message purge."""
        session = MagicMock()
        session.id = "s1"
        session.transcript_path = "/path/to/transcript.jsonl"
        session.external_id = "ext-123"
        session.agent_depth = 0
        session.source = "claude"
        session.digest_markdown = "### Turn 1\nA\n### Turn 2\nB\n### Turn 3\nC"
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]

        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch(
                "gobby.sessions.lifecycle.backup_transcript",
                return_value="/archive/path.gz",
            ),
        ):
            processed = await manager._process_pending_transcripts()

        assert processed == 1

    @pytest.mark.asyncio
    async def test_archive_returns_none(self, manager):
        """When archive returns None, session is still processed."""
        session = MagicMock()
        session.id = "s1"
        session.transcript_path = "/path/to/transcript.jsonl"
        session.external_id = "ext-123"
        session.agent_depth = 0
        session.source = "claude"
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]

        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch(
                "gobby.sessions.lifecycle.backup_transcript",
                return_value=None,
            ),
        ):
            processed = await manager._process_pending_transcripts()

        assert processed == 1

    @pytest.mark.asyncio
    async def test_archive_failure_handled(self, manager):
        """Transcript backup failure doesn't prevent marking as processed."""
        session = MagicMock()
        session.id = "s1"
        session.transcript_path = "/path/to/transcript.jsonl"
        session.external_id = "ext-123"
        session.agent_depth = 0
        session.source = "claude"
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]

        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch(
                "gobby.sessions.lifecycle.backup_transcript",
                side_effect=Exception("Backup failed"),
            ),
        ):
            processed = await manager._process_pending_transcripts()

        assert processed == 1
        manager.session_manager.mark_transcript_processed.assert_called_once()


class TestStartStopIdempotent:
    """Tests for start/stop idempotency."""

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self, manager):
        """Calling start twice doesn't create duplicate tasks."""
        await manager.start()
        task1 = manager._expire_task
        task2 = manager._process_task

        await manager.start()  # Second call should be no-op

        assert manager._expire_task is task1
        assert manager._process_task is task2

        await manager.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self, manager):
        """Calling stop without start is safe."""
        await manager.stop()
        assert manager._expire_task is None
        assert manager._process_task is None
