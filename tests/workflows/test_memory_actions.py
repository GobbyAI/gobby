from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.workflows.definitions import WorkflowState
from gobby.workflows.memory_actions import (
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
