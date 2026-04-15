"""Tests for memory digest pipeline functions.

Relocated from tests/workflows/test_memory_actions.py as part of dead-code cleanup.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.memory.digest import (
    _get_next_turn_number,
    _read_last_turn_from_transcript,
    _read_undigested_turns,
    _synthesize_title,
    build_turn_and_digest,
    memory_sync_export,
    memory_sync_import,
)

pytestmark = pytest.mark.unit


class TestMemorySyncImportDirect:
    """Direct tests for memory_sync_import function."""

    @pytest.mark.asyncio
    async def test_memory_sync_import_no_manager(self):
        """Test memory_sync_import returns error when manager is None."""
        result = await memory_sync_import(None)
        assert result == {"error": "Memory Sync Manager not available"}

    @pytest.mark.asyncio
    async def test_memory_sync_import_success(self):
        """Test memory_sync_import success path."""
        mock_manager = AsyncMock()
        mock_manager.import_from_files.return_value = 5

        result = await memory_sync_import(mock_manager)

        assert result == {"imported": {"memories": 5}}
        mock_manager.import_from_files.assert_awaited_once()


class TestMemorySyncExportDirect:
    """Direct tests for memory_sync_export function."""

    @pytest.mark.asyncio
    async def test_memory_sync_export_no_manager(self):
        """Test memory_sync_export returns error when manager is None."""
        result = await memory_sync_export(None)
        assert result == {"error": "Memory Sync Manager not available"}

    @pytest.mark.asyncio
    async def test_memory_sync_export_success(self):
        """Test memory_sync_export success path."""
        mock_manager = AsyncMock()
        mock_manager.export_to_files.return_value = 7

        result = await memory_sync_export(mock_manager)

        assert result == {"exported": {"memories": 7}}
        mock_manager.export_to_files.assert_awaited_once()


# =============================================================================
# MEMORY INJECT PROJECT CONTEXT TESTS
# =============================================================================


class TestGetNextTurnNumber:
    """Tests for _get_next_turn_number helper."""

    def test_empty_digest(self) -> None:
        assert _get_next_turn_number(None) == 1
        assert _get_next_turn_number("") == 1

    def test_no_turn_headers(self) -> None:
        assert _get_next_turn_number("Some random content\nwithout headers") == 1

    def test_single_turn(self) -> None:
        digest = "### Turn 1\nSome content here"
        assert _get_next_turn_number(digest) == 2

    def test_multiple_turns(self) -> None:
        digest = "### Turn 1\nFirst turn\n\n### Turn 2\nSecond turn\n\n### Turn 3\nThird"
        assert _get_next_turn_number(digest) == 4

    def test_non_sequential_turns(self) -> None:
        digest = "### Turn 1\nFirst\n\n### Turn 5\nFifth"
        assert _get_next_turn_number(digest) == 6


class TestReadLastTurnFromTranscript:
    """Tests for _read_last_turn_from_transcript helper."""

    @pytest.mark.asyncio
    async def test_nonexistent_file(self) -> None:
        prompt, response = await _read_last_turn_from_transcript(
            "/nonexistent/path.jsonl", "claude"
        )
        assert prompt == ""
        assert response == ""

    @pytest.mark.asyncio
    async def test_empty_file(self, tmp_path) -> None:
        jsonl_file = tmp_path / "transcript.jsonl"
        jsonl_file.write_text("")
        prompt, response = await _read_last_turn_from_transcript(str(jsonl_file), "claude")
        assert prompt == ""
        assert response == ""

    @pytest.mark.asyncio
    async def test_claude_transcript(self, tmp_path) -> None:
        """Test reading from a Claude-format JSONL transcript."""
        import json

        jsonl_file = tmp_path / "transcript.jsonl"
        turns = [
            {"message": {"role": "user", "content": "Hello, what is 2+2?"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "2+2 equals 4."}],
                }
            },
        ]
        jsonl_file.write_text("\n".join(json.dumps(t) for t in turns))

        prompt, response = await _read_last_turn_from_transcript(str(jsonl_file), "claude")
        assert prompt == "Hello, what is 2+2?"
        assert response == "2+2 equals 4."

    @pytest.mark.asyncio
    async def test_multiple_turns_returns_last(self, tmp_path) -> None:
        """Test that only the last user/assistant pair is returned."""
        import json

        transcript = tmp_path / "transcript.jsonl"
        lines = [
            {"message": {"role": "user", "content": "Fix the auth bug in login.py"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "I found the issue in login.py line 42. The token validation "
                            "was missing a check for expired tokens. Fixed it.",
                        }
                    ],
                }
            },
            {"message": {"role": "user", "content": "Add tests for the fix"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Created test_login.py with 3 test cases covering "
                            "token expiry, invalid tokens, and valid tokens.",
                        }
                    ],
                }
            },
        ]
        transcript.write_text("\n".join(json.dumps(line) for line in lines))

        prompt, response = await _read_last_turn_from_transcript(str(transcript), "claude")
        assert prompt == "Add tests for the fix"
        assert (
            response
            == "Created test_login.py with 3 test cases covering token expiry, invalid tokens, and valid tokens."
        )


class TestBuildTurnAndDigest:
    """Tests for build_turn_and_digest pipeline function."""

    @pytest.fixture
    def mock_memory_manager(self):
        mm = MagicMock()
        mm.config.enabled = True
        mm.content_exists.return_value = False
        mock_memory = MagicMock()
        mock_memory.id = "mem-123"
        mm.create_memory = AsyncMock(return_value=mock_memory)
        return mm

    @pytest.fixture
    def mock_session_manager(self):
        sm = MagicMock()
        session = MagicMock()
        session.id = "session-123"
        session.transcript_path = None
        session.source = "claude"
        session.digest_markdown = None
        session.title = None
        session.seq_num = 42
        session.terminal_context = None
        sm.get.return_value = session
        sm.update_last_turn_markdown.return_value = session
        sm.update_digest_markdown.return_value = session
        sm.update_title.return_value = session
        return sm

    @pytest.fixture
    def mock_llm_service(self):
        service = MagicMock()
        provider = MagicMock()
        # First call: turn record, second: title synthesis, third: memory extraction
        provider.generate_text = AsyncMock(
            side_effect=[
                "User asked to fix a bug. Agent found the root cause in auth.py line 42.",
                "Fix Auth Bug",
                "NONE",
            ]
        )
        service.get_default_provider.return_value = provider
        return service

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self, mock_session_manager, mock_llm_service):
        mm = MagicMock()
        mm.config.enabled = False
        result = await build_turn_and_digest(
            memory_manager=mm,
            session_manager=mock_session_manager,
            session_id="s1",
            llm_service=mock_llm_service,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_without_llm(self, mock_memory_manager, mock_session_manager):
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="s1",
            llm_service=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_session(self, mock_memory_manager, mock_llm_service):
        sm = MagicMock()
        sm.get.return_value = None
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=sm,
            session_id="nonexistent",
            llm_service=mock_llm_service,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_no_content(
        self, mock_memory_manager, mock_session_manager, mock_llm_service
    ):
        """No transcript and no prompt_text means no content to process."""
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="s1",
            prompt_text=None,
            llm_service=mock_llm_service,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_lifecycle_commands(
        self, mock_memory_manager, mock_session_manager, mock_llm_service
    ):
        for cmd in ["/clear", "/exit", "/compact"]:
            result = await build_turn_and_digest(
                memory_manager=mock_memory_manager,
                session_manager=mock_session_manager,
                session_id="s1",
                prompt_text=cmd,
                llm_service=mock_llm_service,
            )
            assert result is None

    @pytest.mark.asyncio
    @patch("gobby.workflows.summary_actions._rename_tmux_window", new_callable=AsyncMock)
    async def test_successful_pipeline(
        self,
        mock_rename,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Test the full pipeline with prompt_text provided."""
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
        )

        assert result is not None
        assert result["turn_num"] == 1
        assert result["turn_length"] > 0
        assert result["digest_length"] > 0
        assert result["title"] == "Fix Auth Bug"

        # Verify last_turn_markdown was persisted
        mock_session_manager.update_last_turn_markdown.assert_called_once()
        call_args = mock_session_manager.update_last_turn_markdown.call_args
        assert call_args[0][0] == "session-123"
        assert "bug" in call_args[0][1].lower() or "auth" in call_args[0][1].lower()

        # Verify digest was appended with turn header
        mock_session_manager.update_digest_markdown.assert_called_once()
        digest_content = mock_session_manager.update_digest_markdown.call_args[0][1]
        assert "### Turn 1" in digest_content

        # Verify title was updated
        mock_session_manager.update_title.assert_called_once_with("session-123", "Fix Auth Bug")

    @pytest.mark.asyncio
    @patch("gobby.workflows.summary_actions._rename_tmux_window", new_callable=AsyncMock)
    async def test_skips_rename_when_title_is_unchanged(
        self,
        mock_rename,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Already-titled sessions skip title synthesis entirely."""
        session = mock_session_manager.get.return_value
        session.title = "Fix Auth Bug"

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the authentication bug in auth.py",
            llm_service=mock_llm_service,
        )

        assert result is not None
        assert "title" not in result
        mock_session_manager.update_title.assert_not_called()
        mock_rename.assert_not_awaited()
        provider = mock_llm_service.get_default_provider.return_value
        assert provider.generate_text.await_count == 1
        assert provider.generate_text.await_args.kwargs["caller"] == "memory.turn_record"

    @pytest.mark.asyncio
    @patch("gobby.workflows.summary_actions._rename_tmux_window", new_callable=AsyncMock)
    async def test_appends_to_existing_digest(
        self,
        mock_rename,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Test that turns append to existing digest with correct numbering."""
        session = mock_session_manager.get.return_value
        session.digest_markdown = "### Turn 1\nPrevious turn content"

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Next task please",
            llm_service=mock_llm_service,
        )

        assert result is not None
        assert result["turn_num"] == 2

        # Verify digest contains both turns
        digest_content = mock_session_manager.update_digest_markdown.call_args[0][1]
        assert "### Turn 1" in digest_content
        assert "### Turn 2" in digest_content

    @pytest.mark.asyncio
    async def test_digest_config_disabled(
        self, mock_memory_manager, mock_session_manager, mock_llm_service
    ):
        """Test that pipeline respects DigestConfig.enabled = False."""
        config = MagicMock()
        config.digest.enabled = False

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="s1",
            prompt_text="Some prompt",
            llm_service=mock_llm_service,
            config=config,
        )
        assert result is None

    @pytest.mark.asyncio
    @patch("gobby.workflows.summary_actions._rename_tmux_window", new_callable=AsyncMock)
    async def test_reads_from_transcript_when_no_prompt(
        self,
        mock_rename,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
        tmp_path,
    ):
        """Test that transcript is read when prompt_text is None (stop event)."""
        import json

        jsonl_file = tmp_path / "transcript.jsonl"
        turns = [
            {"message": {"role": "user", "content": "Implement the feature"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Done. I implemented it."}],
                }
            },
        ]
        jsonl_file.write_text("\n".join(json.dumps(t) for t in turns))

        session = mock_session_manager.get.return_value
        session.transcript_path = str(jsonl_file)

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=None,  # Simulates stop event
            llm_service=mock_llm_service,
        )

        assert result is not None
        assert result["turn_num"] == 1
        # Verify the LLM was called with transcript content
        provider = mock_llm_service.get_default_provider.return_value
        call_args = provider.generate_text.call_args_list[0]
        prompt = call_args[0][0]
        assert "Implement the feature" in prompt or "feature" in prompt.lower()
        assert call_args.kwargs["caller"] == "memory.turn_record"

    @pytest.mark.asyncio
    async def test_synthesize_title_respects_digest_timeout(self):
        """Digest title synthesis uses digest.timeout to bound LLM latency."""
        provider = MagicMock()
        provider.generate_text = AsyncMock(return_value="unused")
        session_manager = MagicMock()
        session = MagicMock()
        session.title = None
        llm_service = MagicMock()

        async def _slow_call(*args, **kwargs):
            import asyncio as _asyncio

            await _asyncio.sleep(0.05)
            return "Too Slow"

        llm_service.call_feature = AsyncMock(side_effect=_slow_call)
        digest_config = MagicMock(timeout=0.01)

        with pytest.raises(TimeoutError):
            await _synthesize_title(
                provider=provider,
                model=None,
                updated_digest="### Turn 1\nSomething happened",
                session_id="session-123",
                session_manager=session_manager,
                session=session,
                llm_service=llm_service,
                digest_config=digest_config,
            )

        assert llm_service.call_feature.await_args.kwargs["caller"] == "memory.title_synthesis"


class TestBuildTurnAndDigestIdempotency:
    """Tests for digest idempotency via last_digest_input_hash."""

    @pytest.fixture
    def mock_memory_manager(self):
        mm = MagicMock()
        mm.config.enabled = True
        mm.content_exists.return_value = False
        mock_memory = MagicMock()
        mock_memory.id = "mem-123"
        mm.create_memory = AsyncMock(return_value=mock_memory)
        return mm

    @pytest.fixture
    def mock_session_manager(self):
        sm = MagicMock()
        session = MagicMock()
        session.id = "session-123"
        session.transcript_path = None
        session.source = "claude"
        session.digest_markdown = None
        session.title = None
        session.seq_num = 42
        session.terminal_context = None
        session.last_digest_input_hash = None  # No prior digest
        sm.get.return_value = session
        sm.update_last_turn_markdown.return_value = session
        sm.update_digest_markdown.return_value = session
        sm.update_title.return_value = session
        sm.update_last_digest_input_hash.return_value = None
        return sm

    @pytest.fixture
    def mock_llm_service(self):
        service = MagicMock()
        provider = MagicMock()
        provider.generate_text = AsyncMock(
            side_effect=[
                "User asked to fix a bug. Agent found the root cause.",
                "Fix Auth Bug",
                "NONE",
            ]
        )
        service.get_default_provider.return_value = provider
        return service

    @pytest.mark.asyncio
    @patch("gobby.workflows.summary_actions._rename_tmux_window", new_callable=AsyncMock)
    async def test_first_call_processes_and_stores_hash(
        self,
        mock_rename,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """First call should process normally and store the input hash."""
        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the bug",
            llm_service=mock_llm_service,
        )

        assert result is not None
        assert result["turn_num"] == 1
        # Hash should have been persisted
        mock_session_manager.update_last_digest_input_hash.assert_called_once()
        stored_hash = mock_session_manager.update_last_digest_input_hash.call_args[0][1]
        assert len(stored_hash) == 16  # sha256 hex truncated to 16 chars

    @pytest.mark.asyncio
    async def test_duplicate_content_skips(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Second call with same content should skip (return None)."""
        import hashlib

        prompt = "Fix the bug"
        response = ""  # No transcript, no response
        expected_hash = hashlib.sha256(f"{prompt}||{response}".encode()).hexdigest()[:16]

        # Simulate that the hash was already stored from a previous call
        session = mock_session_manager.get.return_value
        session.last_digest_input_hash = expected_hash

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=prompt,
            llm_service=mock_llm_service,
        )

        assert result is None
        # LLM should NOT have been called
        provider = mock_llm_service.get_default_provider.return_value
        provider.generate_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("gobby.workflows.summary_actions._rename_tmux_window", new_callable=AsyncMock)
    async def test_synthesizes_missing_title_from_existing_digest_when_duplicate(
        self,
        mock_rename,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Duplicate digest input still backfills a missing title from existing digest."""
        import hashlib

        prompt = "Fix the bug"
        expected_hash = hashlib.sha256(f"{prompt}||".encode()).hexdigest()[:16]
        session = mock_session_manager.get.return_value
        session.digest_markdown = "### Turn 1\nExisting digest"
        session.last_digest_input_hash = expected_hash

        provider = mock_llm_service.get_default_provider.return_value
        provider.generate_text = AsyncMock(return_value="Recovered Title")

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=prompt,
            llm_service=mock_llm_service,
        )

        assert result == {
            "title": "Recovered Title",
            "title_only": True,
            "digest_length": len(session.digest_markdown),
        }
        mock_session_manager.update_title.assert_called_once_with("session-123", "Recovered Title")
        mock_rename.assert_awaited_once()
        mock_session_manager.update_last_turn_markdown.assert_not_called()
        mock_session_manager.update_digest_markdown.assert_not_called()
        mock_session_manager.update_last_digest_input_hash.assert_not_called()
        assert provider.generate_text.await_count == 1
        assert provider.generate_text.await_args.kwargs["caller"] == "memory.title_synthesis"

    @pytest.mark.asyncio
    async def test_skips_title_synthesis_when_title_present_and_duplicate(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Duplicate digest input does not hit the title LLM when title already exists."""
        import hashlib

        prompt = "Fix the bug"
        expected_hash = hashlib.sha256(f"{prompt}||".encode()).hexdigest()[:16]
        session = mock_session_manager.get.return_value
        session.digest_markdown = "### Turn 1\nExisting digest"
        session.last_digest_input_hash = expected_hash
        session.title = "Existing Title"

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=prompt,
            llm_service=mock_llm_service,
        )

        assert result is None
        provider = mock_llm_service.get_default_provider.return_value
        provider.generate_text.assert_not_called()
        mock_session_manager.update_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_title_synthesis_when_no_digest_and_duplicate(
        self,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Duplicate digest input without an existing digest still skips fully."""
        import hashlib

        prompt = "Fix the bug"
        expected_hash = hashlib.sha256(f"{prompt}||".encode()).hexdigest()[:16]
        session = mock_session_manager.get.return_value
        session.last_digest_input_hash = expected_hash
        session.digest_markdown = None
        session.title = None

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text=prompt,
            llm_service=mock_llm_service,
        )

        assert result is None
        provider = mock_llm_service.get_default_provider.return_value
        provider.generate_text.assert_not_called()
        mock_session_manager.update_title.assert_not_called()

    @pytest.mark.asyncio
    @patch("gobby.workflows.summary_actions._rename_tmux_window", new_callable=AsyncMock)
    async def test_different_content_processes(
        self,
        mock_rename,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Third call with different content should process normally."""
        import hashlib

        # Set hash from a previous different prompt
        old_hash = hashlib.sha256(b"old prompt||old response").hexdigest()[:16]
        session = mock_session_manager.get.return_value
        session.last_digest_input_hash = old_hash

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Now add tests for the fix",
            llm_service=mock_llm_service,
        )

        assert result is not None
        assert result["turn_num"] == 1
        # New hash should have been stored
        mock_session_manager.update_last_digest_input_hash.assert_called_once()
        new_hash = mock_session_manager.update_last_digest_input_hash.call_args[0][1]
        assert new_hash != old_hash


class TestReadUndigestedTurns:
    """Tests for _read_undigested_turns function."""

    def _write_claude_transcript(self, path, exchanges):
        """Write a Claude-format JSONL transcript with given exchanges.

        Each exchange is a (user_text, assistant_text) tuple.
        If assistant_text is None, only the user turn is written (interrupted).
        """
        import json

        with open(path, "w") as f:
            for user_text, assistant_text in exchanges:
                user_turn = {
                    "type": "user",
                    "message": {"role": "user", "content": user_text},
                }
                f.write(json.dumps(user_turn) + "\n")
                if assistant_text is not None:
                    assistant_turn = {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": assistant_text}],
                        },
                    }
                    f.write(json.dumps(assistant_turn) + "\n")

    @pytest.mark.asyncio
    async def test_nonexistent_file(self) -> None:
        """Returns empty list for missing transcript."""
        result = await _read_undigested_turns("/nonexistent/path.jsonl", "claude", 0)
        assert result == []

    @pytest.mark.asyncio
    async def test_single_pair_backward_compat(self, tmp_path) -> None:
        """With 1 pair and 0 digested, returns that single pair."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(transcript, [("Hello", "Hi there")])

        result = await _read_undigested_turns(str(transcript), "claude", 0)
        assert len(result) == 1
        assert result[0][0] == "Hello"
        assert result[0][1] == "Hi there"

    @pytest.mark.asyncio
    async def test_catches_missed_turns(self, tmp_path) -> None:
        """With 3 pairs and 1 digested, returns 2 undigested."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [
                ("First question", "First answer"),
                ("Second question", "Second answer"),
                ("Third question", "Third answer"),
            ],
        )

        result = await _read_undigested_turns(str(transcript), "claude", 1)
        assert len(result) == 2
        assert result[0][0] == "Second question"
        assert result[0][1] == "Second answer"
        assert result[1][0] == "Third question"
        assert result[1][1] == "Third answer"

    @pytest.mark.asyncio
    async def test_lifecycle_commands_filtered(self, tmp_path) -> None:
        """Lifecycle commands like /clear are excluded from pairs."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [
                ("Real question", "Real answer"),
                ("/compact", "Compacted"),
                ("Another question", "Another answer"),
            ],
        )

        result = await _read_undigested_turns(str(transcript), "claude", 0)
        assert len(result) == 2
        assert result[0][0] == "Real question"
        assert result[1][0] == "Another question"

    @pytest.mark.asyncio
    async def test_clear_boundary(self, tmp_path) -> None:
        """Only reads post-/clear content."""
        transcript = tmp_path / "transcript.jsonl"
        import json

        with open(transcript, "w") as f:
            # Pre-clear exchange
            f.write(
                json.dumps({"type": "user", "message": {"role": "user", "content": "Old question"}})
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Old answer"}],
                        },
                    }
                )
                + "\n"
            )
            # /clear boundary
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": "<command-name>/clear</command-name>",
                        },
                    }
                )
                + "\n"
            )
            # Post-clear exchange
            f.write(
                json.dumps({"type": "user", "message": {"role": "user", "content": "New question"}})
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "New answer"}],
                        },
                    }
                )
                + "\n"
            )

        result = await _read_undigested_turns(str(transcript), "claude", 0)
        assert len(result) == 1
        assert result[0][0] == "New question"
        assert result[0][1] == "New answer"

    @pytest.mark.asyncio
    async def test_interrupted_turn_pairs_with_empty_response(self, tmp_path) -> None:
        """An interrupted turn (user without assistant) gets empty response."""
        transcript = tmp_path / "transcript.jsonl"
        import json

        with open(transcript, "w") as f:
            # First user message (interrupted - no assistant response)
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "Interrupted question"},
                    }
                )
                + "\n"
            )
            # Second user message (new message after interrupt)
            f.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "Follow-up question"},
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Final answer"}],
                        },
                    }
                )
                + "\n"
            )

        result = await _read_undigested_turns(str(transcript), "claude", 0)
        assert len(result) == 2
        assert result[0] == ("Interrupted question", "")
        assert result[1] == ("Follow-up question", "Final answer")

    @pytest.mark.asyncio
    async def test_all_digested_falls_back_to_last(self, tmp_path) -> None:
        """When digested_count >= len(pairs), returns last pair as fallback."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [("Q1", "A1"), ("Q2", "A2")],
        )

        # Claim 5 are digested but only 2 exist (e.g., /clear reset)
        result = await _read_undigested_turns(str(transcript), "claude", 5)
        assert len(result) == 1
        assert result[0] == ("Q2", "A2")


class TestBuildTurnAndDigestCatchUp:
    """Tests for build_turn_and_digest catching up on missed turns."""

    @pytest.fixture
    def mock_memory_manager(self):
        mm = MagicMock()
        mm.config.enabled = True
        mm.content_exists.return_value = False
        mock_memory = MagicMock()
        mock_memory.id = "mem-456"
        mm.create_memory = AsyncMock(return_value=mock_memory)
        return mm

    @pytest.fixture
    def mock_llm_service(self):
        service = MagicMock()
        provider = MagicMock()
        provider.generate_text = AsyncMock(
            side_effect=[
                "User asked two questions. Agent answered both.",
                "Multi-Exchange Session",
                "NONE",
            ]
        )
        service.get_default_provider.return_value = provider
        return service

    def _write_claude_transcript(self, path, exchanges):
        """Write a Claude-format JSONL transcript."""
        import json

        with open(path, "w") as f:
            for user_text, assistant_text in exchanges:
                f.write(
                    json.dumps({"type": "user", "message": {"role": "user", "content": user_text}})
                    + "\n"
                )
                if assistant_text is not None:
                    f.write(
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": assistant_text}],
                                },
                            }
                        )
                        + "\n"
                    )

    @pytest.mark.asyncio
    @patch("gobby.workflows.summary_actions._rename_tmux_window", new_callable=AsyncMock)
    async def test_catches_up_missed_turns(
        self,
        mock_rename,
        mock_memory_manager,
        mock_llm_service,
        tmp_path,
    ):
        """Session with 1 digested turn + 2 undigested: digest has Turn 2 covering both."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [
                ("First question", "First answer"),
                ("Second question", "Second answer"),
                ("Third question", "Third answer"),
            ],
        )

        sm = MagicMock()
        session = MagicMock()
        session.id = "session-456"
        session.transcript_path = str(transcript)
        session.source = "claude"
        session.digest_markdown = "### Turn 1\nFirst turn already digested"
        session.seq_num = 99
        session.terminal_context = None
        session.last_digest_input_hash = None
        sm.get.return_value = session
        sm.update_last_turn_markdown.return_value = session
        sm.update_digest_markdown.return_value = session
        sm.update_title.return_value = session
        sm.update_last_digest_input_hash.return_value = None

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=sm,
            session_id="session-456",
            llm_service=mock_llm_service,
        )

        assert result is not None
        assert result["turn_num"] == 2

        # Verify the LLM was called with multi-exchange content
        provider = mock_llm_service.get_default_provider.return_value
        turn_prompt_call = provider.generate_text.call_args_list[0]
        prompt_text = turn_prompt_call[0][0]
        assert "Exchange 1" in prompt_text
        assert "Exchange 2" in prompt_text
        assert "Second question" in prompt_text
        assert "Third question" in prompt_text
        assert turn_prompt_call.kwargs["caller"] == "memory.turn_record"

        # Verify digest contains both turns
        digest_content = sm.update_digest_markdown.call_args[0][1]
        assert "### Turn 1" in digest_content
        assert "### Turn 2" in digest_content

    @pytest.mark.asyncio
    async def test_idempotency_combined_hash(
        self,
        mock_memory_manager,
        mock_llm_service,
        tmp_path,
    ):
        """Same batch of undigested pairs doesn't re-process."""
        import hashlib

        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [
                ("First question", "First answer"),
                ("Second question", "Second answer"),
            ],
        )

        # Compute the expected hash for the 2 undigested pairs
        combined = "First question||First answer||Second question||Second answer"
        expected_hash = hashlib.sha256(combined.encode()).hexdigest()[:16]

        sm = MagicMock()
        session = MagicMock()
        session.id = "session-456"
        session.transcript_path = str(transcript)
        session.source = "claude"
        session.digest_markdown = None  # 0 digested
        session.seq_num = 99
        session.terminal_context = None
        session.last_digest_input_hash = expected_hash  # Already processed
        sm.get.return_value = session

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=sm,
            session_id="session-456",
            llm_service=mock_llm_service,
        )

        assert result is None
        # LLM should NOT have been called
        provider = mock_llm_service.get_default_provider.return_value
        provider.generate_text.assert_not_called()
