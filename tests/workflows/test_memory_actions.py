from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.workflows.definitions import WorkflowState
from gobby.workflows.memory_actions import (
    _get_next_turn_number,
    _read_last_turn_from_transcript,
    _read_undigested_turns,
    build_turn_and_digest,
    generate_session_boundary_summaries,
    memory_extract_from_session,
    memory_inject_project_context,
    memory_recall_relevant,
    memory_recall_with_synthesis,
    memory_review_gate,
    memory_save,
    memory_sync_export,
    memory_sync_import,
)

pytestmark = pytest.mark.unit


# =============================================================================
# DIRECT FUNCTION TESTS - Testing memory_actions.py functions directly
# These tests bypass ActionExecutor to directly test the functions
# =============================================================================


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


class TestMemorySaveDirect:
    """Direct tests for memory_save function."""

    @pytest.mark.asyncio
    async def test_memory_save_no_memory_manager(self):
        """Test memory_save returns error when memory_manager is None."""
        result = await memory_save(
            memory_manager=None,
            session_manager=MagicMock(),
            session_id="test-session",
            content="test content",
        )
        assert result == {"error": "Memory Manager not available"}

    @pytest.mark.asyncio
    async def test_memory_save_config_disabled(self):
        """Test memory_save returns None when config.enabled is False."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = False

        result = await memory_save(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            content="test content",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_save_no_project_id(self):
        """Test memory_save returns error when no project_id found."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True

        mock_session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = None
        mock_session_manager.get.return_value = mock_session

        result = await memory_save(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="test-session",
            content="test content",
        )
        assert result == {"error": "No project_id found"}

    @pytest.mark.asyncio
    async def test_memory_save_session_not_found_no_project(self):
        """Test memory_save returns error when session not found and no project_id."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True

        mock_session_manager = MagicMock()
        mock_session_manager.get.return_value = None

        result = await memory_save(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="test-session",
            content="test content",
        )
        assert result == {"error": "No project_id found"}

    @pytest.mark.asyncio
    async def test_memory_save_normalizes_invalid_memory_type(self):
        """Test memory_save normalizes invalid memory_type to 'fact'."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True
        mock_memory_manager.content_exists.return_value = False
        mock_memory_manager.create_memory = AsyncMock()
        mock_memory = MagicMock()
        mock_memory.id = "mem-123"
        mock_memory_manager.create_memory.return_value = mock_memory

        result = await memory_save(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            content="test content",
            memory_type="invalid_type",
            project_id="proj-123",
        )

        assert result is not None
        assert result["saved"] is True
        assert result["memory_type"] == "fact"

    @pytest.mark.asyncio
    async def test_memory_save_normalizes_invalid_tags(self):
        """Test memory_save normalizes invalid tags to empty list."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True
        mock_memory_manager.content_exists.return_value = False
        mock_memory_manager.create_memory = AsyncMock()
        mock_memory = MagicMock()
        mock_memory.id = "mem-123"
        mock_memory_manager.create_memory.return_value = mock_memory

        result = await memory_save(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            content="test content",
            tags="not a list",
            project_id="proj-123",
        )

        assert result is not None
        assert result["saved"] is True
        call_kwargs = mock_memory_manager.create_memory.call_args[1]
        assert call_kwargs["tags"] == []

    @pytest.mark.asyncio
    async def test_memory_save_exception_handling(self):
        """Test memory_save handles exceptions gracefully."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True
        mock_memory_manager.content_exists.return_value = False
        mock_memory_manager.create_memory = AsyncMock(side_effect=Exception("DB error"))

        result = await memory_save(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            content="test content",
            project_id="proj-123",
        )

        assert result is not None
        assert "error" in result
        assert "DB error" in result["error"]


class TestMemoryRecallRelevantDirect:
    """Direct tests for memory_recall_relevant function."""

    @pytest.mark.asyncio
    async def test_memory_recall_relevant_no_memory_manager(self):
        """Test memory_recall_relevant returns None when memory_manager is None."""
        result = await memory_recall_relevant(
            memory_manager=None,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="test prompt",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_recall_relevant_config_disabled(self):
        """Test memory_recall_relevant returns None when config.enabled is False."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = False

        result = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="test prompt",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_recall_relevant_no_prompt_text(self):
        """Test memory_recall_relevant returns None when prompt_text is None."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True

        result = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_memory_recall_relevant_resolves_project_from_session(self):
        """Test memory_recall_relevant resolves project_id from session."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True

        m1 = MagicMock()
        m1.memory_type = "fact"
        m1.content = "Test memory"
        mock_memory_manager.search_memories = AsyncMock(return_value=[m1])

        mock_session_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-from-session"
        mock_session_manager.get.return_value = mock_session

        result = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="test-session",
            prompt_text="a longer prompt text here",
        )

        assert result is not None
        call_kwargs = mock_memory_manager.search_memories.call_args[1]
        assert call_kwargs["project_id"] == "proj-from-session"

    @pytest.mark.asyncio
    async def test_memory_recall_relevant_exception_handling(self):
        """Test memory_recall_relevant handles exceptions gracefully."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True
        mock_memory_manager.search_memories = AsyncMock(side_effect=Exception("Search error"))

        result = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="a longer prompt text here",
            project_id="proj-123",
        )

        assert result is not None
        assert "error" in result
        assert "Search error" in result["error"]


# Additional edge case tests for improved coverage


class TestMemoryRecallRelevantEdgeCases:
    """Additional edge case tests for memory_recall_relevant."""

    @pytest.mark.asyncio
    async def test_memory_recall_relevant_session_not_found_uses_none_project(self):
        """Test memory_recall_relevant when session not found and no explicit project_id."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True

        mock_session_manager = MagicMock()
        mock_session_manager.get.return_value = None  # Session not found

        m1 = MagicMock()
        m1.memory_type = "fact"
        m1.content = "Test memory"
        mock_memory_manager.search_memories = AsyncMock(return_value=[m1])

        result = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="test-session",
            prompt_text="a longer prompt text here",
            project_id=None,  # No explicit project_id
        )

        assert result is not None
        # Verify recall was called with None project_id
        call_kwargs = mock_memory_manager.search_memories.call_args[1]
        assert call_kwargs["project_id"] is None


# =============================================================================
# MEMORY DEDUPLICATION TESTS
# =============================================================================


class TestMemoryDeduplication:
    """Tests for memory injection deduplication per session."""

    @pytest.mark.asyncio
    async def test_memory_recall_tracks_injected_ids_in_state(self):
        """Test that memory_recall_relevant tracks injected memory IDs in state."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True

        m1 = MagicMock()
        m1.id = "mem-001"
        m1.memory_type = "fact"
        m1.content = "Test memory 1"
        m2 = MagicMock()
        m2.id = "mem-002"
        m2.memory_type = "fact"
        m2.content = "Test memory 2"
        mock_memory_manager.search_memories = AsyncMock(return_value=[m1, m2])

        state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
        )

        result = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="a longer prompt text here",
            project_id="proj-123",
            state=state,
        )

        assert result is not None
        assert result["injected"] is True
        assert result["count"] == 2

        # Verify IDs were tracked in state
        assert "_injected_memory_ids" in state.variables
        assert set(state.variables["_injected_memory_ids"]) == {"mem-001", "mem-002"}

    @pytest.mark.asyncio
    async def test_memory_recall_deduplicates_on_second_call(self):
        """Test that second call with same memories returns empty."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True

        m1 = MagicMock()
        m1.id = "mem-001"
        m1.memory_type = "fact"
        m1.content = "Test memory"
        mock_memory_manager.search_memories = AsyncMock(return_value=[m1])

        state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
        )

        # First call
        result1 = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="first prompt text here",
            project_id="proj-123",
            state=state,
        )

        assert result1 is not None
        assert result1["injected"] is True
        assert result1["count"] == 1

        # Second call with same memory
        result2 = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="second prompt text here",
            project_id="proj-123",
            state=state,
        )

        assert result2 is not None
        assert result2["injected"] is False
        assert result2["count"] == 0
        assert result2.get("skipped") == 1

    @pytest.mark.asyncio
    async def test_memory_recall_allows_new_memories_after_first_call(self):
        """Test that new memories are still injected on subsequent calls."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True

        m1 = MagicMock()
        m1.id = "mem-001"
        m1.memory_type = "fact"
        m1.content = "Test memory 1"

        m2 = MagicMock()
        m2.id = "mem-002"
        m2.memory_type = "fact"
        m2.content = "Test memory 2"

        state = WorkflowState(
            session_id="test-session",
            workflow_name="test-workflow",
            step="test-step",
        )

        # First call - returns m1
        mock_memory_manager.search_memories = AsyncMock(return_value=[m1])
        result1 = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="first prompt text here",
            project_id="proj-123",
            state=state,
        )

        assert result1["count"] == 1

        # Second call - returns m1 and m2, but only m2 is new
        mock_memory_manager.search_memories = AsyncMock(return_value=[m1, m2])
        result2 = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="second prompt text here",
            project_id="proj-123",
            state=state,
        )

        assert result2 is not None
        assert result2["injected"] is True
        assert result2["count"] == 1  # Only m2 is new

        # Verify both IDs are now tracked
        assert set(state.variables["_injected_memory_ids"]) == {"mem-001", "mem-002"}

    @pytest.mark.asyncio
    async def test_memory_recall_works_without_state(self):
        """Test that memory_recall_relevant works when state is None (no dedup)."""
        mock_memory_manager = MagicMock()
        mock_memory_manager.config.enabled = True

        m1 = MagicMock()
        m1.id = "mem-001"
        m1.memory_type = "fact"
        m1.content = "Test memory"
        mock_memory_manager.search_memories = AsyncMock(return_value=[m1])

        # Call without state - should work without deduplication
        result = await memory_recall_relevant(
            memory_manager=mock_memory_manager,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="prompt text here for test",
            project_id="proj-123",
            state=None,
        )

        assert result is not None
        assert result["injected"] is True
        assert result["count"] == 1


# =============================================================================
# MEMORY REVIEW GATE TESTS
# =============================================================================


class TestMemoryReviewGate:
    """Tests for memory_review_gate function."""

    @pytest.mark.asyncio
    async def test_blocks_when_pending_review(self):
        """Test gate blocks when pending_memory_review is true and clears the flag."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        state = WorkflowState(session_id="test-session", workflow_name="test", step="test")
        state.variables = {"pending_memory_review": True}

        result = await memory_review_gate(
            memory_manager=mock_mm,
            session_id="test-session",
            state=state,
        )

        assert result is not None
        assert result["decision"] == "block"
        assert "create_memory" in result["reason"]
        # Gate should self-clear to prevent infinite loops
        assert state.variables["pending_memory_review"] is False

    @pytest.mark.asyncio
    async def test_allows_when_no_pending_review(self):
        """Test gate allows stop when no pending review."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        state = WorkflowState(session_id="test-session", workflow_name="test", step="test")
        state.variables = {"pending_memory_review": False}

        result = await memory_review_gate(
            memory_manager=mock_mm,
            session_id="test-session",
            state=state,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_allows_when_no_state(self):
        """Test gate allows stop when state is None."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        result = await memory_review_gate(
            memory_manager=mock_mm,
            session_id="test-session",
            state=None,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_allows_when_memory_disabled(self):
        """Test gate allows stop when memory is disabled."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = False

        state = WorkflowState(session_id="test-session", workflow_name="test", step="test")
        state.variables = {"pending_memory_review": True}

        result = await memory_review_gate(
            memory_manager=mock_mm,
            session_id="test-session",
            state=state,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_allows_when_no_memory_manager(self):
        """Test gate allows stop when memory_manager is None."""
        result = await memory_review_gate(
            memory_manager=None,
            session_id="test-session",
            state=MagicMock(),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_self_clearing_allows_second_stop(self):
        """Test gate fires once then allows the next stop attempt."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        state = WorkflowState(session_id="test-session", workflow_name="test", step="test")
        state.variables = {"pending_memory_review": True}

        # First call: blocks and clears the flag
        result = await memory_review_gate(
            memory_manager=mock_mm,
            session_id="test-session",
            state=state,
        )
        assert result is not None
        assert result["decision"] == "block"

        # Second call: flag cleared, allows stop
        result = await memory_review_gate(
            memory_manager=mock_mm,
            session_id="test-session",
            state=state,
        )
        assert result is None


# =============================================================================
# MEMORY EXTRACT FROM SESSION TESTS
# =============================================================================


class TestMemoryExtractFromSession:
    """Tests for memory_extract_from_session function."""

    @pytest.mark.asyncio
    @patch("gobby.memory.extractor.SessionMemoryExtractor")
    async def test_extracts_memories(self, mock_extractor_cls):
        """Test successful memory extraction from session."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        mock_sm = MagicMock()
        mock_llm = MagicMock()

        from gobby.memory.extractor import MemoryCandidate

        candidates = [
            MemoryCandidate(
                content="Use uv run for development",
                memory_type="fact",
                tags=["development"],
            ),
        ]

        mock_extractor = AsyncMock()
        mock_extractor.extract.return_value = candidates
        mock_extractor_cls.return_value = mock_extractor

        result = await memory_extract_from_session(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            llm_service=mock_llm,
            transcript_processor=None,
            session_id="test-session",
        )

        assert result is not None
        assert result["extracted"] == 1
        assert len(result["memories"]) == 1

    @pytest.mark.asyncio
    async def test_returns_error_without_memory_manager(self):
        """Test returns error when memory_manager is None."""
        result = await memory_extract_from_session(
            memory_manager=None,
            session_manager=MagicMock(),
            llm_service=MagicMock(),
            transcript_processor=None,
            session_id="test-session",
        )

        assert result is not None
        assert "error" in result

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        """Test returns None when memory is disabled."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = False

        result = await memory_extract_from_session(
            memory_manager=mock_mm,
            session_manager=MagicMock(),
            llm_service=MagicMock(),
            transcript_processor=None,
            session_id="test-session",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_error_without_llm(self):
        """Test returns error when llm_service is None."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        result = await memory_extract_from_session(
            memory_manager=mock_mm,
            session_manager=MagicMock(),
            llm_service=None,
            transcript_processor=None,
            session_id="test-session",
        )

        assert result is not None
        assert "error" in result

    @pytest.mark.asyncio
    @patch("gobby.memory.extractor.SessionMemoryExtractor")
    async def test_handles_extraction_error(self, mock_extractor_cls):
        """Test graceful handling of extraction errors."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        mock_sm = MagicMock()
        mock_llm = MagicMock()

        mock_extractor = AsyncMock()
        mock_extractor.extract.side_effect = Exception("LLM error")
        mock_extractor_cls.return_value = mock_extractor

        result = await memory_extract_from_session(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            llm_service=mock_llm,
            transcript_processor=None,
            session_id="test-session",
        )

        assert result is not None
        assert "error" in result


# =============================================================================
# MEMORY INJECT PROJECT CONTEXT TESTS
# =============================================================================


class TestMemoryInjectProjectContext:
    """Tests for memory_inject_project_context function."""

    @pytest.mark.asyncio
    async def test_injects_project_memories(self):
        """Test successful injection of project memories."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        m1 = MagicMock()
        m1.id = "mem-001"
        m1.memory_type = "fact"
        m1.content = "Use uv run for development"
        m2 = MagicMock()
        m2.id = "mem-002"
        m2.memory_type = "preference"
        m2.content = "Prefer bun over npm"
        mock_mm.list_memories.return_value = [m1, m2]

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_sm.get.return_value = mock_session

        state = WorkflowState(session_id="test-session", workflow_name="test", step="test")

        result = await memory_inject_project_context(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            session_id="test-session",
            state=state,
        )

        assert result is not None
        assert result["injected"] is True
        assert result["count"] == 2
        assert "inject_context" in result

        # Verify list_memories was called with project_id
        mock_mm.list_memories.assert_called_once_with(
            project_id="proj-123",
            limit=10,
        )

        # Verify IDs tracked in state
        assert set(state.variables["_injected_memory_ids"]) == {"mem-001", "mem-002"}

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        """Test returns None when memory is disabled."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = False

        result = await memory_inject_project_context(
            memory_manager=mock_mm,
            session_manager=MagicMock(),
            session_id="test-session",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_manager(self):
        """Test returns None when memory_manager is None."""
        result = await memory_inject_project_context(
            memory_manager=None,
            session_manager=MagicMock(),
            session_id="test-session",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_project(self):
        """Test returns None when session has no project_id."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = None
        mock_sm.get.return_value = mock_session

        result = await memory_inject_project_context(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            session_id="test-session",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_memories(self):
        """Test returns empty result when no memories found."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True
        mock_mm.list_memories.return_value = []

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_sm.get.return_value = mock_session

        result = await memory_inject_project_context(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            session_id="test-session",
        )

        assert result is not None
        assert result["injected"] is False
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_deduplicates_already_injected(self):
        """Test skips memories already injected in this session."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        m1 = MagicMock()
        m1.id = "mem-001"
        m1.memory_type = "fact"
        m1.content = "Already injected"
        mock_mm.list_memories.return_value = [m1]

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_sm.get.return_value = mock_session

        state = WorkflowState(session_id="test-session", workflow_name="test", step="test")
        state.variables = {"_injected_memory_ids": ["mem-001"]}

        result = await memory_inject_project_context(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            session_id="test-session",
            state=state,
        )

        assert result is not None
        assert result["injected"] is False
        assert result["count"] == 0
        assert result.get("skipped") == 1

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """Test graceful handling of errors."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True
        mock_mm.list_memories.side_effect = Exception("DB error")

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_sm.get.return_value = mock_session

        result = await memory_inject_project_context(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            session_id="test-session",
        )

        assert result is not None
        assert "error" in result


# =============================================================================
# MEMORY RECALL WITH SYNTHESIS TESTS (rewritten: current-prompt search)
# =============================================================================


class TestMemoryRecallWithSynthesis:
    """Tests for the rewritten memory_recall_with_synthesis."""

    @pytest.mark.asyncio
    async def test_searches_using_current_prompt(self):
        """memory_recall_with_synthesis searches based on current prompt, not stale synthesis."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        m1 = MagicMock()
        m1.id = "mem-001"
        m1.memory_type = "fact"
        m1.content = "Python uses indentation"
        mock_mm.search_memories = AsyncMock(return_value=[m1])

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_session.digest_markdown = None
        mock_sm.get.return_value = mock_session

        result = await memory_recall_with_synthesis(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            session_id="test-session",
            prompt_text="How does Python handle indentation?",
        )

        assert result is not None
        assert result["injected"] is True
        assert result["count"] == 1
        mock_mm.search_memories.assert_called_once()
        call_kwargs = mock_mm.search_memories.call_args[1]
        assert "indentation" in call_kwargs["query"]

    @pytest.mark.asyncio
    async def test_enriches_query_with_digest(self):
        """memory_recall_with_synthesis enriches search query with session digest."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True
        mock_mm.search_memories = AsyncMock(return_value=[])

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_session.digest_markdown = "**Task**: Working on memory system\n**Domain**: memory"
        mock_sm.get.return_value = mock_session

        await memory_recall_with_synthesis(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            session_id="test-session",
            prompt_text="Fix the staleness bug",
        )

        call_kwargs = mock_mm.search_memories.call_args[1]
        query = call_kwargs["query"]
        assert "Fix the staleness bug" in query
        assert "memory system" in query

    @pytest.mark.asyncio
    async def test_fallback_to_prompt_only_without_digest(self):
        """memory_recall_with_synthesis uses prompt-only when no digest available."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True
        mock_mm.search_memories = AsyncMock(return_value=[])

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_session.digest_markdown = None
        mock_sm.get.return_value = mock_session

        await memory_recall_with_synthesis(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            session_id="test-session",
            prompt_text="What is the architecture?",
        )

        call_kwargs = mock_mm.search_memories.call_args[1]
        assert call_kwargs["query"] == "What is the architecture?"

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        """memory_recall_with_synthesis returns None when memory disabled."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = False

        result = await memory_recall_with_synthesis(
            memory_manager=mock_mm,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="test prompt text here",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_skips_short_prompts(self):
        """memory_recall_with_synthesis skips very short prompts."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        result = await memory_recall_with_synthesis(
            memory_manager=mock_mm,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="hi",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_skips_slash_commands(self):
        """memory_recall_with_synthesis skips slash commands."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        result = await memory_recall_with_synthesis(
            memory_manager=mock_mm,
            session_manager=MagicMock(),
            session_id="test-session",
            prompt_text="/clear session",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_deduplication_via_state(self):
        """memory_recall_with_synthesis passes state for deduplication."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True

        m1 = MagicMock()
        m1.id = "mem-001"
        m1.memory_type = "fact"
        m1.content = "Test fact"
        mock_mm.search_memories = AsyncMock(return_value=[m1])

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_session.digest_markdown = None
        mock_sm.get.return_value = mock_session

        state = WorkflowState(
            session_id="test-session",
            workflow_name="test",
            step="test",
        )

        result = await memory_recall_with_synthesis(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            session_id="test-session",
            prompt_text="some longer prompt text",
            state=state,
        )

        assert result is not None
        assert result["count"] == 1
        assert "mem-001" in state.variables.get("_injected_memory_ids", [])

    @pytest.mark.asyncio
    async def test_uses_limit_parameter(self):
        """memory_recall_with_synthesis passes limit to memory_recall_relevant."""
        mock_mm = MagicMock()
        mock_mm.config.enabled = True
        mock_mm.search_memories = AsyncMock(return_value=[])

        mock_sm = MagicMock()
        mock_session = MagicMock()
        mock_session.project_id = "proj-123"
        mock_session.digest_markdown = None
        mock_sm.get.return_value = mock_session

        await memory_recall_with_synthesis(
            memory_manager=mock_mm,
            session_manager=mock_sm,
            session_id="test-session",
            prompt_text="test query with enough length",
            limit=7,
        )

        call_kwargs = mock_mm.search_memories.call_args[1]
        assert call_kwargs["limit"] == 7


# =============================================================================
# TURN-BY-TURN DIGEST PIPELINE TESTS
# =============================================================================


class TestGetNextTurnNumber:
    """Tests for _get_next_turn_number helper."""

    def test_empty_digest(self):
        assert _get_next_turn_number(None) == 1
        assert _get_next_turn_number("") == 1

    def test_no_turn_headers(self):
        assert _get_next_turn_number("Some random content\nwithout headers") == 1

    def test_single_turn(self):
        digest = "### Turn 1\nSome content here"
        assert _get_next_turn_number(digest) == 2

    def test_multiple_turns(self):
        digest = "### Turn 1\nFirst turn\n\n### Turn 2\nSecond turn\n\n### Turn 3\nThird"
        assert _get_next_turn_number(digest) == 4

    def test_non_sequential_turns(self):
        digest = "### Turn 1\nFirst\n\n### Turn 5\nFifth"
        assert _get_next_turn_number(digest) == 6


class TestReadLastTurnFromTranscript:
    """Tests for _read_last_turn_from_transcript helper."""

    def test_nonexistent_file(self):
        prompt, response = _read_last_turn_from_transcript("/nonexistent/path.jsonl", "claude")
        assert prompt == ""
        assert response == ""

    def test_empty_file(self, tmp_path):
        jsonl_file = tmp_path / "transcript.jsonl"
        jsonl_file.write_text("")
        prompt, response = _read_last_turn_from_transcript(str(jsonl_file), "claude")
        assert prompt == ""
        assert response == ""

    def test_claude_transcript(self, tmp_path):
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

        prompt, response = _read_last_turn_from_transcript(str(jsonl_file), "claude")
        assert prompt == "Hello, what is 2+2?"
        assert response == "2+2 equals 4."

    def test_multiple_turns_returns_last(self, tmp_path):
        """Test that only the last user/assistant pair is returned."""
        import json

        jsonl_file = tmp_path / "transcript.jsonl"
        turns = [
            {"message": {"role": "user", "content": "First question"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "First answer"}],
                }
            },
            {"message": {"role": "user", "content": "Second question"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Second answer"}],
                }
            },
        ]
        jsonl_file.write_text("\n".join(json.dumps(t) for t in turns))

        prompt, response = _read_last_turn_from_transcript(str(jsonl_file), "claude")
        assert prompt == "Second question"
        assert response == "Second answer"


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
        session.jsonl_path = None
        session.source = "claude"
        session.digest_markdown = None
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
    @patch("gobby.workflows.summary_actions._rename_tmux_window", new_callable=AsyncMock)
    async def test_memory_extraction_from_turn(
        self,
        mock_rename,
        mock_memory_manager,
        mock_session_manager,
        mock_llm_service,
    ):
        """Test that memories are extracted when LLM returns candidates."""
        provider = mock_llm_service.get_default_provider.return_value
        provider.generate_text = AsyncMock(
            side_effect=[
                "Agent fixed auth bug in auth.py by adding token validation.",
                "Fix Auth Bug",
                '{"content": "auth.py requires token validation on line 42", "memory_type": "fact", "tags": ["auth", "debugging"]}',
            ]
        )

        # Mock the storage lookup for project_id
        mock_memory_manager.storage.db.fetchone.return_value = {"project_id": "proj-1"}

        result = await build_turn_and_digest(
            memory_manager=mock_memory_manager,
            session_manager=mock_session_manager,
            session_id="session-123",
            prompt_text="Fix the auth bug",
            llm_service=mock_llm_service,
        )

        assert result is not None
        assert result.get("memories_extracted", 0) == 1
        mock_memory_manager.create_memory.assert_awaited_once()

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
        session.jsonl_path = str(jsonl_file)

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


class TestGenerateSessionBoundarySummaries:
    """Tests for generate_session_boundary_summaries."""

    @pytest.fixture
    def mock_session_manager(self):
        sm = MagicMock()
        session = MagicMock()
        session.digest_markdown = (
            "### Turn 1\nUser asked to fix authentication bug. "
            "Agent found root cause in auth.py line 42 and applied fix.\n\n"
            "### Turn 2\nUser asked to add tests. Agent created test_auth.py with 5 test cases."
        )
        sm.get.return_value = session
        sm.update_compact_markdown.return_value = session
        sm.update_summary.return_value = session
        return sm

    @pytest.fixture
    def mock_llm_service(self):
        service = MagicMock()
        provider = MagicMock()
        provider.generate_text = AsyncMock(
            return_value=(
                "## Output A: Handoff Context\n"
                "Working on auth system refactor.\n\n"
                "===SECTION_BREAK===\n\n"
                "## Output B: Session Summary\n"
                "Refactored auth module, added token validation."
            )
        )
        service.get_default_provider.return_value = provider
        return service

    @pytest.mark.asyncio
    async def test_returns_none_without_session(self, mock_llm_service):
        sm = MagicMock()
        sm.get.return_value = None
        result = await generate_session_boundary_summaries(
            session_id="s1", session_manager=sm, llm_service=mock_llm_service
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_without_digest(self, mock_llm_service):
        sm = MagicMock()
        session = MagicMock()
        session.digest_markdown = None
        sm.get.return_value = session
        result = await generate_session_boundary_summaries(
            session_id="s1", session_manager=sm, llm_service=mock_llm_service
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_with_short_digest(self, mock_llm_service):
        sm = MagicMock()
        session = MagicMock()
        session.digest_markdown = "Short"
        sm.get.return_value = session
        result = await generate_session_boundary_summaries(
            session_id="s1", session_manager=sm, llm_service=mock_llm_service
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_boundary_generation(self, mock_session_manager, mock_llm_service):
        result = await generate_session_boundary_summaries(
            session_id="s1",
            session_manager=mock_session_manager,
            llm_service=mock_llm_service,
        )

        assert result is not None
        assert result["compact_length"] > 0
        assert result["summary_length"] > 0

        # Verify both were persisted
        mock_session_manager.update_compact_markdown.assert_called_once()
        mock_session_manager.update_summary.assert_called_once()

        # Verify the section split worked
        compact = mock_session_manager.update_compact_markdown.call_args[0][1]
        assert "auth" in compact.lower() or "Handoff" in compact

    @pytest.mark.asyncio
    async def test_fallback_splits_on_output_b_header(self, mock_session_manager):
        """When LLM omits ===SECTION_BREAK=== but includes Output B header, split there."""
        service = MagicMock()
        provider = MagicMock()
        provider.generate_text = AsyncMock(
            return_value=(
                "## Output A: Handoff Context\n"
                "Working on auth refactor.\n\n"
                "## Output B: Session Summary\n"
                "Completed auth module refactor."
            )
        )
        service.get_default_provider.return_value = provider

        result = await generate_session_boundary_summaries(
            session_id="s1",
            session_manager=mock_session_manager,
            llm_service=service,
        )

        assert result is not None
        compact = mock_session_manager.update_compact_markdown.call_args[0][1]
        summary = mock_session_manager.update_summary.call_args[1].get(
            "summary_markdown"
        ) or mock_session_manager.update_summary.call_args[0][1]

        # compact should NOT contain Output B content
        assert "Session Summary" not in compact
        assert "Completed auth" not in compact
        # summary should contain Output B content
        assert "Completed auth" in summary

    @pytest.mark.asyncio
    async def test_fallback_no_header_truncates_compact(self, mock_session_manager):
        """When no marker and no Output B header, summary gets full response."""
        service = MagicMock()
        provider = MagicMock()
        provider.generate_text = AsyncMock(
            return_value="Full summary without any separator or headers."
        )
        service.get_default_provider.return_value = provider

        result = await generate_session_boundary_summaries(
            session_id="s1",
            session_manager=mock_session_manager,
            llm_service=service,
        )

        assert result is not None
        mock_session_manager.update_compact_markdown.assert_called_once()
        mock_session_manager.update_summary.assert_called_once()
        summary_arg = mock_session_manager.update_summary.call_args
        summary_text = summary_arg[1].get("summary_markdown") or summary_arg[0][1]
        assert "Full summary" in summary_text

    @pytest.mark.asyncio
    async def test_handles_llm_error(self, mock_session_manager):
        service = MagicMock()
        provider = MagicMock()
        provider.generate_text = AsyncMock(side_effect=Exception("LLM unavailable"))
        service.get_default_provider.return_value = provider

        result = await generate_session_boundary_summaries(
            session_id="s1",
            session_manager=mock_session_manager,
            llm_service=service,
        )

        assert result is not None
        assert "error" in result


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
        session.jsonl_path = None
        session.source = "claude"
        session.digest_markdown = None
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

    def test_nonexistent_file(self):
        """Returns empty list for missing transcript."""
        result = _read_undigested_turns("/nonexistent/path.jsonl", "claude", 0)
        assert result == []

    def test_single_pair_backward_compat(self, tmp_path):
        """With 1 pair and 0 digested, returns that single pair."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(transcript, [("Hello", "Hi there")])

        result = _read_undigested_turns(str(transcript), "claude", 0)
        assert len(result) == 1
        assert result[0][0] == "Hello"
        assert result[0][1] == "Hi there"

    def test_catches_missed_turns(self, tmp_path):
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

        result = _read_undigested_turns(str(transcript), "claude", 1)
        assert len(result) == 2
        assert result[0][0] == "Second question"
        assert result[0][1] == "Second answer"
        assert result[1][0] == "Third question"
        assert result[1][1] == "Third answer"

    def test_lifecycle_commands_filtered(self, tmp_path):
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

        result = _read_undigested_turns(str(transcript), "claude", 0)
        assert len(result) == 2
        assert result[0][0] == "Real question"
        assert result[1][0] == "Another question"

    def test_clear_boundary(self, tmp_path):
        """Only reads post-/clear content."""
        transcript = tmp_path / "transcript.jsonl"
        import json

        with open(transcript, "w") as f:
            # Pre-clear exchange
            f.write(
                json.dumps(
                    {"type": "user", "message": {"role": "user", "content": "Old question"}}
                )
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
                json.dumps(
                    {"type": "user", "message": {"role": "user", "content": "New question"}}
                )
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

        result = _read_undigested_turns(str(transcript), "claude", 0)
        assert len(result) == 1
        assert result[0][0] == "New question"
        assert result[0][1] == "New answer"

    def test_interrupted_turn_pairs_with_empty_response(self, tmp_path):
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

        result = _read_undigested_turns(str(transcript), "claude", 0)
        assert len(result) == 2
        assert result[0] == ("Interrupted question", "")
        assert result[1] == ("Follow-up question", "Final answer")

    def test_all_digested_falls_back_to_last(self, tmp_path):
        """When digested_count >= len(pairs), returns last pair as fallback."""
        transcript = tmp_path / "transcript.jsonl"
        self._write_claude_transcript(
            transcript,
            [("Q1", "A1"), ("Q2", "A2")],
        )

        # Claim 5 are digested but only 2 exist (e.g., /clear reset)
        result = _read_undigested_turns(str(transcript), "claude", 5)
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
                    json.dumps(
                        {"type": "user", "message": {"role": "user", "content": user_text}}
                    )
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
        session.jsonl_path = str(transcript)
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
        session.jsonl_path = str(transcript)
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
