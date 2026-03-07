import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.storage.session_models import Session

pytestmark = pytest.mark.unit


class TestSessionLifecycleManager:
    """Tests for SessionLifecycleManager."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def mock_config(self):
        config = MagicMock(spec=SessionLifecycleConfig)
        config.expire_check_interval_minutes = 1
        config.transcript_processing_interval_minutes = 1
        config.active_session_pause_minutes = 30
        config.stale_session_timeout_hours = 24
        config.transcript_processing_batch_size = 10
        return config

    @pytest.fixture
    def manager(self, mock_db, mock_config):
        """Create manager with mocked dependencies."""
        with patch("gobby.sessions.lifecycle.LocalSessionManager") as MockSessionManager:
            with patch("gobby.sessions.lifecycle.LocalSessionMessageManager") as MockMessageManager:
                manager = SessionLifecycleManager(mock_db, mock_config)
                manager.session_manager = MockSessionManager.return_value
                manager.message_manager = MockMessageManager.return_value
                return manager

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

        count = await manager._expire_stale_sessions()

        assert count == 6
        manager.session_manager.pause_inactive_active_sessions.assert_called_once_with(
            timeout_minutes=manager.config.active_session_pause_minutes
        )
        manager.session_manager.expire_orphaned_handoff_sessions.assert_called_once_with(
            timeout_minutes=30
        )
        manager.session_manager.expire_stale_sessions.assert_called_once_with(
            timeout_hours=manager.config.stale_session_timeout_hours
        )

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
        session.jsonl_path = str(tmp_path / "transcript.jsonl")

        manager.session_manager.get_pending_transcript_sessions.return_value = [session]

        # Create real file content
        with open(session.jsonl_path, "w") as f:
            f.write('{"type": "message", "content": "hello"}\n')

        # Mock _process_session_transcript to avoid complex parsing logic
        # OR mock the components inside it.
        # Let's mock _process_session_transcript to focus on the loop logic first.
        with patch.object(
            manager, "_process_session_transcript", new_callable=AsyncMock
        ) as mock_process:
            processed = await manager._process_pending_transcripts()

            assert processed == 1
            mock_process.assert_awaited_once_with("s1", session.jsonl_path)
            manager.session_manager.mark_transcript_processed.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_process_session_transcript_real_parsing(self, tmp_path, manager):
        """Test parsing logic inside _process_session_transcript."""
        jsonl_path = tmp_path / "transcript.jsonl"
        with open(jsonl_path, "w") as f:
            f.write(
                '{"type": "message", "message": {"content": "hello"}, "timestamp": "2024-01-01T00:00:00Z"}\n'
            )

        message_mock = MagicMock()
        message_mock.index = 5

        with patch("gobby.sessions.lifecycle.ClaudeTranscriptParser") as MockParser:
            parser_instance = MockParser.return_value
            parser_instance.parse_lines.return_value = [message_mock]

            manager.message_manager.store_messages = AsyncMock()
            manager.message_manager.update_state = AsyncMock()

            await manager._process_session_transcript("s1", str(jsonl_path))

            manager.message_manager.store_messages.assert_awaited_once()
            manager.message_manager.update_state.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_process_session_transcript_missing_file(self, manager):
        """Test handling of missing file."""
        await manager._process_session_transcript("s1", "/non/existent/file.jsonl")
        # Should just return without error
        manager.message_manager.store_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_session_transcript_read_error(self, tmp_path, manager):
        """Test error reading transcript file."""
        jsonl_path = tmp_path / "transcript.jsonl"
        with open(jsonl_path, "w") as f:
            f.write("content")

        # Permission error mock or similar
        with patch("builtins.open", side_effect=OSError("Read error")):
            with pytest.raises(IOError):
                await manager._process_session_transcript("s1", str(jsonl_path))

    @pytest.mark.asyncio
    async def test_process_session_transcript_empty_file(self, tmp_path, manager):
        """Test processing empty file."""
        jsonl_path = tmp_path / "transcript.jsonl"
        jsonl_path.touch()

        await manager._process_session_transcript("s1", str(jsonl_path))

        manager.message_manager.store_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_session_transcript_no_messages(self, tmp_path, manager):
        """Test file with no valid messages."""
        jsonl_path = tmp_path / "transcript.jsonl"
        with open(jsonl_path, "w") as f:
            f.write('{"type": "unknown"}\n')

        with patch("gobby.sessions.lifecycle.ClaudeTranscriptParser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []

            await manager._process_session_transcript("s1", str(jsonl_path))

            manager.message_manager.store_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_pending_transcripts_loop_error(self, manager):
        """Test error handling in process loop (single iteration logic)."""
        manager.session_manager.get_pending_transcript_sessions.side_effect = Exception("DB Error")

        # Should propagate or handle? _process_pending_transcripts does NOT catch its own top-level errors (the loop does)
        with pytest.raises(Exception, match="DB Error"):
            await manager._process_pending_transcripts()

    @pytest.mark.asyncio
    async def test_process_pending_transcripts_individual_error(self, manager):
        """Test error handling for individual session processing."""
        s1 = MagicMock(id="s1")
        s2 = MagicMock(id="s2")
        manager.session_manager.get_pending_transcript_sessions.return_value = [s1, s2]

        # Mock _process_session_transcript to fail for first, succeed for second
        with patch.object(
            manager, "_process_session_transcript", new_callable=AsyncMock
        ) as mock_proc:
            mock_proc.side_effect = [Exception("Fail"), None]

            processed = await manager._process_pending_transcripts()

            assert processed == 1
            assert mock_proc.call_count == 2


class TestBackgroundLoops:
    """Tests for infinite background loops."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def mock_config(self):
        config = MagicMock(spec=SessionLifecycleConfig)
        config.expire_check_interval_minutes = 1
        config.transcript_processing_interval_minutes = 1
        config.active_session_pause_minutes = 30
        config.stale_session_timeout_hours = 24
        config.transcript_processing_batch_size = 10
        return config

    @pytest.fixture
    def manager(self, mock_db, mock_config):
        """Create manager with mocked dependencies."""
        with patch("gobby.sessions.lifecycle.LocalSessionManager") as MockSessionManager:
            with patch("gobby.sessions.lifecycle.LocalSessionMessageManager") as MockMessageManager:
                manager = SessionLifecycleManager(mock_db, mock_config)
                manager.session_manager = MockSessionManager.return_value
                manager.message_manager = MockMessageManager.return_value
                return manager

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

    @pytest.mark.asyncio
    async def test_loops_handle_cancellation(self, manager):
        """Test loops exit on CancelledError during sleep."""
        manager._running = True
        manager._expire_stale_sessions = AsyncMock()

        with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            await manager._expire_loop()
            # Should just return cleanly
            assert True


class TestPromptFileCleanup:
    """Tests for _cleanup_prompt_files (#7389)."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def mock_config(self):
        config = MagicMock(spec=SessionLifecycleConfig)
        config.expire_check_interval_minutes = 1
        config.transcript_processing_interval_minutes = 1
        config.active_session_pause_minutes = 30
        config.stale_session_timeout_hours = 24
        config.transcript_processing_batch_size = 10
        return config

    @pytest.fixture
    def manager(self, mock_db, mock_config):
        with patch("gobby.sessions.lifecycle.LocalSessionManager"):
            with patch("gobby.sessions.lifecycle.LocalSessionMessageManager"):
                return SessionLifecycleManager(mock_db, mock_config)

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

        with patch.object(manager, "_cleanup_prompt_files") as mock_cleanup:
            await manager._expire_stale_sessions()

        mock_cleanup.assert_called_once()
