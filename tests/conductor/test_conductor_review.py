"""Tests for ConductorManager pipeline review integration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.conductor.manager import ConductorManager
from gobby.config.conductor import ConductorConfig
from gobby.workflows.pipeline_state import (
    ExecutionStatus,
    PipelineExecution,
    StepExecution,
    StepStatus,
)

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000001"


def _make_execution(
    execution_id: str = "pe-abc123",
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
) -> PipelineExecution:
    return PipelineExecution(
        id=execution_id,
        pipeline_name="test-pipeline",
        project_id=PROJECT_ID,
        status=status,
        created_at="2026-03-22T10:00:00+00:00",
        updated_at="2026-03-22T10:02:25+00:00",
        completed_at="2026-03-22T10:02:25+00:00",
    )


def _make_step(step_id: str = "build") -> StepExecution:
    return StepExecution(
        id=1,
        execution_id="pe-abc123",
        step_id=step_id,
        status=StepStatus.COMPLETED,
        started_at="2026-03-22T10:00:00+00:00",
        completed_at="2026-03-22T10:00:10+00:00",
    )


def _make_manager(
    execution_manager: MagicMock | None = None,
) -> ConductorManager:
    cfg = ConductorConfig(enabled=True, model="haiku")
    session_manager = MagicMock()
    mock_session = MagicMock()
    mock_session.id = "db-session-id"
    mock_session.seq_num = 1
    session_manager.register.return_value = mock_session

    return ConductorManager(
        project_id=PROJECT_ID,
        project_path="/tmp/test-project",
        session_manager=session_manager,
        config=cfg,
        execution_manager=execution_manager,
    )


def _mock_chat_session(response_json: dict | None = None) -> MagicMock:
    """Create a mock ChatSession that returns a JSON review response."""
    from gobby.llm.claude_models import DoneEvent, TextChunk

    session = MagicMock()
    session.is_connected = True
    session.start = AsyncMock()
    session.stop = AsyncMock()

    response_text = json.dumps(response_json) if response_json else '{"summary": "ok"}'

    async def mock_send_message(content: str):
        yield TextChunk(content=response_text)
        yield DoneEvent(
            tool_calls_count=0,
            input_tokens=100,
            output_tokens=20,
            duration_ms=500,
        )

    session.send_message = mock_send_message
    return session


class TestReviewCompletedPipelines:
    """Tests for _review_completed_pipelines."""

    @pytest.mark.asyncio
    async def test_no_op_without_execution_manager(self) -> None:
        manager = _make_manager(execution_manager=None)
        result = await manager._review_completed_pipelines()
        assert result is None

    @pytest.mark.asyncio
    async def test_no_op_when_no_unreviewed(self) -> None:
        exec_mgr = MagicMock()
        exec_mgr.get_unreviewed_completions.return_value = []

        manager = _make_manager(execution_manager=exec_mgr)
        result = await manager._review_completed_pipelines()
        assert result is None

    @pytest.mark.asyncio
    async def test_reviews_completed_execution(self) -> None:
        execution = _make_execution()
        steps = [_make_step("build"), _make_step("test")]

        exec_mgr = MagicMock()
        exec_mgr.get_unreviewed_completions.return_value = [execution]
        exec_mgr.get_steps_for_execution.return_value = steps
        exec_mgr.store_review = MagicMock()

        llm_response = {
            "quality_signals": [{"signal": "ok", "detail": "all good"}],
            "suggestions": ["none"],
            "summary": "Clean execution",
        }

        manager = _make_manager(execution_manager=exec_mgr)
        manager._session = _mock_chat_session(llm_response)

        result = await manager._review_completed_pipelines()

        assert result == "Reviewed 1 execution(s)"
        exec_mgr.store_review.assert_called_once()
        stored_review = json.loads(exec_mgr.store_review.call_args[0][1])
        assert stored_review["summary"] == "Clean execution"
        assert len(stored_review["timeline"]) == 2

    @pytest.mark.asyncio
    async def test_stores_structured_data_on_llm_failure(self) -> None:
        """When LLM returns unparseable response, structured data is still stored."""
        execution = _make_execution()
        steps = [_make_step("build")]

        exec_mgr = MagicMock()
        exec_mgr.get_unreviewed_completions.return_value = [execution]
        exec_mgr.get_steps_for_execution.return_value = steps
        exec_mgr.store_review = MagicMock()

        # LLM returns non-JSON
        from gobby.llm.claude_models import DoneEvent, TextChunk

        bad_session = MagicMock()
        bad_session.is_connected = True

        async def bad_send(content: str):
            yield TextChunk(content="I cannot produce JSON right now")
            yield DoneEvent(
                tool_calls_count=0,
                input_tokens=50,
                output_tokens=10,
                duration_ms=200,
            )

        bad_session.send_message = bad_send

        manager = _make_manager(execution_manager=exec_mgr)
        manager._session = bad_session

        result = await manager._review_completed_pipelines()

        assert result == "Reviewed 1 execution(s)"
        exec_mgr.store_review.assert_called_once()
        stored_review = json.loads(exec_mgr.store_review.call_args[0][1])
        # Structured data present, LLM fields empty
        assert stored_review["timeline"][0]["step_id"] == "build"
        assert stored_review["quality_signals"] == []
        assert stored_review["suggestions"] == []

    @pytest.mark.asyncio
    async def test_caps_at_five_reviews_per_tick(self) -> None:
        exec_mgr = MagicMock()
        exec_mgr.get_unreviewed_completions.return_value = []  # Will be checked for limit arg

        manager = _make_manager(execution_manager=exec_mgr)
        await manager._review_completed_pipelines()

        exec_mgr.get_unreviewed_completions.assert_called_once_with(limit=5)

    @pytest.mark.asyncio
    async def test_continues_on_individual_review_failure(self) -> None:
        """If one execution fails to review, others still proceed."""
        exec1 = _make_execution(execution_id="pe-001")
        exec2 = _make_execution(execution_id="pe-002")

        exec_mgr = MagicMock()
        exec_mgr.get_unreviewed_completions.return_value = [exec1, exec2]
        # First call raises, second succeeds
        exec_mgr.get_steps_for_execution.side_effect = [
            RuntimeError("DB error"),
            [_make_step("build")],
        ]
        exec_mgr.store_review = MagicMock()

        manager = _make_manager(execution_manager=exec_mgr)
        manager._session = _mock_chat_session({"summary": "ok"})

        result = await manager._review_completed_pipelines()

        assert result == "Reviewed 1 execution(s)"
        assert exec_mgr.store_review.call_count == 1


class TestGetLlmReview:
    """Tests for _get_llm_review."""

    @pytest.mark.asyncio
    async def test_parses_json_response(self) -> None:
        manager = _make_manager()
        expected = {"summary": "test", "quality_signals": []}
        manager._session = _mock_chat_session(expected)

        result = await manager._get_llm_review("Review this pipeline")

        assert result == expected

    @pytest.mark.asyncio
    async def test_returns_none_on_non_json(self) -> None:
        from gobby.llm.claude_models import DoneEvent, TextChunk

        manager = _make_manager()
        session = MagicMock()

        async def send(content: str):
            yield TextChunk(content="Not JSON at all")
            yield DoneEvent(
                tool_calls_count=0,
                input_tokens=10,
                output_tokens=5,
                duration_ms=100,
            )

        session.send_message = send
        manager._session = session

        result = await manager._get_llm_review("Review this")
        assert result is None

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self) -> None:
        from gobby.llm.claude_models import DoneEvent, TextChunk

        manager = _make_manager()
        session = MagicMock()

        async def send(content: str):
            yield TextChunk(content='```json\n{"summary": "fenced"}\n```')
            yield DoneEvent(
                tool_calls_count=0,
                input_tokens=10,
                output_tokens=5,
                duration_ms=100,
            )

        session.send_message = send
        manager._session = session

        result = await manager._get_llm_review("Review this")
        assert result == {"summary": "fenced"}

    @pytest.mark.asyncio
    async def test_returns_none_without_session(self) -> None:
        manager = _make_manager()
        manager._session = None

        result = await manager._get_llm_review("Review this")
        assert result is None
