"""Tests for sessions/summarize.py — shared session summary generation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.summarize import generate_session_summaries

pytestmark = pytest.mark.unit


def _make_session(
    session_id: str = "sess-1",
    jsonl_path: str | None = None,
    source: str = "claude",
    summary_markdown: str | None = None,
    compact_markdown: str | None = None,
) -> MagicMock:
    session = MagicMock()
    session.id = session_id
    session.jsonl_path = jsonl_path
    session.source = source
    session.summary_markdown = summary_markdown
    session.compact_markdown = compact_markdown
    return session


def _write_transcript(tmp_path: Path) -> str:
    """Write a minimal JSONL transcript and return its path."""
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        {
            "type": "human",
            "message": {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi there!"}]},
        },
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in lines))
    return str(transcript)


class TestGenerateSessionSummaries:
    """Tests for generate_session_summaries()."""

    @pytest.mark.asyncio
    async def test_no_session_manager(self) -> None:
        result = await generate_session_summaries(session_id="s1", session_manager=None)
        assert result["success"] is False
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_session_not_found(self) -> None:
        sm = MagicMock()
        sm.get.return_value = None
        result = await generate_session_summaries(session_id="s1", session_manager=sm)
        assert result["success"] is False
        assert "No session found" in result["error"]

    @pytest.mark.asyncio
    async def test_no_transcript_path(self) -> None:
        sm = MagicMock()
        sm.get.return_value = _make_session(jsonl_path=None)
        result = await generate_session_summaries(session_id="s1", session_manager=sm)
        assert result["success"] is False
        assert "No transcript path" in result["error"]

    @pytest.mark.asyncio
    async def test_transcript_not_found(self) -> None:
        sm = MagicMock()
        sm.get.return_value = _make_session(jsonl_path="/nonexistent/path.jsonl")
        result = await generate_session_summaries(session_id="s1", session_manager=sm)
        assert result["success"] is False
        assert "Transcript file not found" in result["error"]

    @pytest.mark.asyncio
    async def test_compact_only(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(jsonl_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="# Compact Summary\nHello world.",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                compact_only=True,
            )

        assert result["success"] is True
        assert result["compact_length"] > 0
        assert result["full_length"] == 0
        sm.update_compact_markdown.assert_called_once()
        sm.update_summary.assert_not_called()

    @pytest.mark.asyncio
    async def test_sets_handoff_ready(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(jsonl_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.sessions.formatting.format_handoff_as_markdown", return_value="# Summary"),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                compact_only=True,
                set_handoff_ready=True,
            )

        assert result["success"] is True
        sm.update_status.assert_called_once_with("sess-1", "handoff_ready")

    @pytest.mark.asyncio
    async def test_skips_handoff_ready_when_disabled(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(jsonl_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.sessions.formatting.format_handoff_as_markdown", return_value="# Summary"),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                compact_only=True,
                set_handoff_ready=False,
            )

        assert result["success"] is True
        sm.update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_summary_with_llm(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(jsonl_path=transcript_path)

        mock_provider = AsyncMock()
        mock_provider.generate_summary.return_value = "# Full Summary\nDetails here."

        mock_llm = MagicMock()
        mock_llm.get_default_provider.return_value = mock_provider

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch("gobby.sessions.formatting.format_handoff_as_markdown", return_value="# Compact"),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=("# Full Summary", None),
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                llm_service=mock_llm,
            )

        assert result["success"] is True
        assert result["full_length"] > 0
        assert result["compact_length"] > 0

    @pytest.mark.asyncio
    async def test_full_only_error_returns_failure(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(jsonl_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.summarize._generate_full_summary", return_value=(None, "LLM error")
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                full_only=True,
            )

        assert result["success"] is False
        assert "LLM error" in result["error"]


class TestGetClaimedTasks:
    """Tests for _get_claimed_tasks()."""

    def test_returns_empty_on_no_tasks(self) -> None:
        """Returns empty string when session has no tasks."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_db = MagicMock()
        with patch("gobby.storage.session_tasks.SessionTaskManager") as MockSTM:
            MockSTM.return_value.get_session_tasks.return_value = []
            result = _get_claimed_tasks("sess-1", mock_db)
        assert result == ""

    def test_formats_task_with_seq_num(self) -> None:
        """Formats tasks with seq_num refs and descriptions."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_task = MagicMock()
        mock_task.id = "task-uuid-1234"
        mock_task.seq_num = 42
        mock_task.status = "in_progress"
        mock_task.title = "Fix the bug"
        mock_task.description = "A short description"

        mock_db = MagicMock()
        with (
            patch("gobby.storage.session_tasks.SessionTaskManager") as MockSTM,
            patch("gobby.storage.task_dependencies.TaskDependencyManager") as MockDep,
        ):
            MockSTM.return_value.get_session_tasks.return_value = [{"task": mock_task}]
            MockDep.return_value.get_all_dependencies.return_value = []
            result = _get_claimed_tasks("sess-1", mock_db)

        assert "#42" in result
        assert "[in_progress]" in result
        assert "Fix the bug" in result
        assert "A short description" in result
