"""Tests for sessions/summarize.py — shared session summary generation."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.summarize import (
    TRANSCRIPT_FALLBACK_MAX_CHARS,
    TRANSCRIPT_FALLBACK_MAX_TURNS,
    _generate_full_summary,
    generate_session_summaries,
)
from gobby.storage.database import LocalDatabase
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def _make_session(
    session_id: str = "sess-1",
    transcript_path: str | None = None,
    source: str = "claude",
    summary_markdown: str | None = None,
    digest_markdown: str | None = None,
    last_turn_markdown: str | None = None,
    last_assistant_content: str | None = None,
) -> MagicMock:
    session = MagicMock()
    session.id = session_id
    session.transcript_path = transcript_path
    session.source = source
    session.summary_markdown = summary_markdown
    session.digest_markdown = digest_markdown
    session.last_turn_markdown = last_turn_markdown
    session.last_assistant_content = last_assistant_content
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
    async def test_repeated_summary_persistence_keeps_sqlite_connections_bounded(
        self,
        temp_db: LocalDatabase,
    ) -> None:
        """Session get/update_summary/update_status calls use the bounded DB runner."""
        sm = SessionManager(temp_db)
        project = LocalProjectManager(temp_db).create(
            name="summary-project",
            repo_path="/tmp/summary-project",
        )
        session = sm.register(
            external_id="summary-bounded-db",
            machine_id="machine-1",
            source="codex",
            project_id=project.id,
        )
        sm.update_digest_markdown(session.id, "### Turn 1\nUse digest context.")
        executor = DatabaseExecutor(max_workers=2, thread_name_prefix="summary-db")
        original_get = SessionManager.get

        def slow_get(self, *args, **kwargs):
            time.sleep(0.02)
            return original_get(self, *args, **kwargs)

        try:
            with (
                patch.object(SessionManager, "get", new=slow_get),
                patch("gobby.sessions.summarize._enrich_git_context"),
                patch(
                    "gobby.sessions.summarize._generate_full_summary",
                    return_value=("# Summary", None),
                ),
            ):
                results = await asyncio.gather(
                    *(
                        generate_session_summaries(
                            session_id=session.id,
                            session_manager=sm,
                            db=temp_db,
                            run_db=executor.run,
                        )
                        for _ in range(20)
                    )
                )

            assert all(result["success"] is True for result in results)
            assert temp_db.connection_count <= 1 + executor.max_workers
        finally:
            executor.shutdown(wait=True)

    @pytest.mark.asyncio
    async def test_no_transcript_path(self) -> None:
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=None)
        result = await generate_session_summaries(session_id="s1", session_manager=sm)
        assert result["success"] is False
        assert "No transcript path" in result["error"]

    @pytest.mark.asyncio
    async def test_transcript_not_found(self) -> None:
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path="/nonexistent/path.jsonl")
        result = await generate_session_summaries(session_id="s1", session_manager=sm)
        assert result["success"] is False
        assert "Transcript file not found" in result["error"]

    @pytest.mark.asyncio
    async def test_compact_only(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

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
        # compact_only is ignored — always generates full summary via fallback
        assert result["full_length"] > 0

    @pytest.mark.asyncio
    async def test_sets_handoff_ready(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

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
        sm.get.return_value = _make_session(transcript_path=transcript_path)

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
        sm.get.return_value = _make_session(transcript_path=transcript_path)

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

    @pytest.mark.asyncio
    async def test_full_only_error_returns_failure(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.summarize._generate_full_summary", return_value=(None, "LLM error")
            ),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="# Fallback Summary",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                full_only=True,
            )

        # full_only flag is ignored — fallback to code-only renderer on LLM error
        assert result["success"] is True
        assert result["full_length"] > 0

    @pytest.mark.asyncio
    async def test_full_summary_uses_droid_parser_with_transcript_path(self) -> None:
        session = _make_session(
            session_id="sess-droid",
            transcript_path="/tmp/droid-session.jsonl",
            source="droid",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = ""
        session_manager = MagicMock()
        session_manager.db = None
        provider = AsyncMock()
        provider.generate_summary.return_value = "# Droid Summary"

        with (
            patch("gobby.sessions.summarize._resolve_provider", return_value=provider),
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.sessions.transcripts.droid.DroidTranscriptParser") as MockParser,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.workflows.summary_actions._format_structured_context",
                return_value="structured",
            ),
            patch("gobby.workflows.summary_actions.format_turns_for_llm", return_value="turns"),
        ):
            MockPromptLoader.return_value.load.return_value.content = "prompt"
            MockParser.return_value.extract_turns_since_clear.return_value = [{"type": "message"}]
            MockParser.return_value.extract_last_messages.return_value = [
                {"role": "user", "content": "hi"}
            ]

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[{"type": "message"}],
                handoff_ctx=handoff_ctx,
                llm_service=None,
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == "# Droid Summary"
        assert full_error is None
        MockParser.assert_called_once_with(
            session_id="sess-droid",
            transcript_path="/tmp/droid-session.jsonl",
        )

    @pytest.mark.asyncio
    async def test_digest_primary_context_does_not_format_full_transcript(self) -> None:
        session = _make_session(
            session_id="sess-digest",
            transcript_path="/tmp/transcript.jsonl",
            digest_markdown="### Turn 1\nDigest is the bounded source.",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = "clean"
        session_manager = MagicMock()
        session_manager.db = None
        provider = AsyncMock()
        provider.generate_summary.return_value = "# Digest Summary"

        with (
            patch("gobby.sessions.summarize._resolve_provider", return_value=provider),
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.workflows.summary_actions._format_structured_context",
                return_value="structured",
            ),
            patch("gobby.workflows.summary_actions.format_turns_for_llm") as mock_format,
        ):
            MockPromptLoader.return_value.load.return_value.content = "prompt"

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[{"message": {"role": "user", "content": "raw transcript"}}],
                handoff_ctx=handoff_ctx,
                llm_service=None,
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == "# Digest Summary"
        assert full_error is None
        mock_format.assert_not_called()
        context = provider.generate_summary.await_args.args[0]
        assert context["transcript_summary"] == "### Turn 1\nDigest is the bounded source."
        assert context["last_messages"] == "### Turn 1\nDigest is the bounded source."

    @pytest.mark.asyncio
    async def test_digest_primary_context_includes_latest_turn_when_digest_lags(self) -> None:
        session = _make_session(
            session_id="sess-digest",
            transcript_path="/tmp/transcript.jsonl",
            digest_markdown="### Turn 1\nOld coordinator state.",
            last_turn_markdown="Current build state: #12746 is development:in_progress.",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = "clean"
        session_manager = MagicMock()
        session_manager.db = None
        provider = AsyncMock()
        provider.generate_summary.return_value = "# Digest Summary"

        with (
            patch("gobby.sessions.summarize._resolve_provider", return_value=provider),
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.workflows.summary_actions._format_structured_context",
                return_value="structured",
            ),
            patch("gobby.workflows.summary_actions.format_turns_for_llm") as mock_format,
        ):
            MockPromptLoader.return_value.load.return_value.content = "prompt"

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[{"message": {"role": "user", "content": "raw transcript"}}],
                handoff_ctx=handoff_ctx,
                llm_service=None,
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == "# Digest Summary"
        assert full_error is None
        mock_format.assert_not_called()
        context = provider.generate_summary.await_args.args[0]
        assert "Old coordinator state." in context["transcript_summary"]
        assert (
            "Current build state: #12746 is development:in_progress."
            in context["transcript_summary"]
        )
        assert context["last_messages"].endswith(
            "Current build state: #12746 is development:in_progress."
        )

    @pytest.mark.asyncio
    async def test_digest_primary_context_includes_current_assistant_content(self) -> None:
        session = _make_session(
            session_id="sess-digest",
            transcript_path="/tmp/transcript.jsonl",
            digest_markdown="### Turn 1\nOld coordinator state.",
            last_turn_markdown="Old coordinator state.",
            last_assistant_content="Current handoff: #14997 open and #12746 still running.",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = "clean"
        session_manager = MagicMock()
        session_manager.db = None
        provider = AsyncMock()
        provider.generate_summary.return_value = "# Digest Summary"

        with (
            patch("gobby.sessions.summarize._resolve_provider", return_value=provider),
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.workflows.summary_actions._format_structured_context",
                return_value="structured",
            ),
            patch("gobby.workflows.summary_actions.format_turns_for_llm") as mock_format,
        ):
            MockPromptLoader.return_value.load.return_value.content = "prompt"

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[{"message": {"role": "user", "content": "raw transcript"}}],
                handoff_ctx=handoff_ctx,
                llm_service=None,
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == "# Digest Summary"
        assert full_error is None
        mock_format.assert_not_called()
        context = provider.generate_summary.await_args.args[0]
        assert "Old coordinator state." in context["transcript_summary"]
        assert (
            "Current handoff: #14997 open and #12746 still running."
            in context["transcript_summary"]
        )
        assert context["last_messages"].endswith(
            "Current handoff: #14997 open and #12746 still running."
        )

    @pytest.mark.asyncio
    async def test_full_summary_enrichment_uses_run_db(self) -> None:
        session = _make_session(
            session_id="sess-enrich",
            transcript_path="/tmp/transcript.jsonl",
            digest_markdown="### Turn 1\nDigest source.",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = ""
        session_manager = MagicMock()
        session_manager.db = MagicMock()
        provider = AsyncMock()
        provider.generate_summary.return_value = "# Enriched Summary"
        run_db_calls = []

        async def run_db(func, *args, **kwargs):
            run_db_calls.append(func)
            return func(*args, **kwargs)

        with (
            patch("gobby.sessions.summarize._resolve_provider", return_value=provider),
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.workflows.summary_actions._format_structured_context",
                return_value="structured",
            ),
            patch(
                "gobby.sessions.summarize._get_claimed_tasks", return_value="task context"
            ) as claimed,
            patch(
                "gobby.sessions.summarize._get_session_memories",
                return_value="memory context",
            ) as memories,
        ):
            MockPromptLoader.return_value.load.return_value.content = "prompt"

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[],
                handoff_ctx=handoff_ctx,
                llm_service=None,
                db=session_manager.db,
                session_manager=session_manager,
                run_db=run_db,
            )

        assert full_markdown == "# Enriched Summary"
        assert full_error is None
        assert claimed in run_db_calls
        assert memories in run_db_calls
        context = provider.generate_summary.await_args.args[0]
        assert context["claimed_tasks"] == "task context"
        assert context["session_memories"] == "memory context"

    @pytest.mark.asyncio
    async def test_missing_digest_uses_bounded_transcript_fallback(self) -> None:
        session = _make_session(session_id="sess-fallback", transcript_path="/tmp/transcript.jsonl")
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = ""
        session_manager = MagicMock()
        session_manager.db = None
        provider = AsyncMock()
        provider.generate_summary.return_value = "# Transcript Summary"
        turns = [{"idx": i} for i in range(TRANSCRIPT_FALLBACK_MAX_TURNS + 20)]
        formatted = "fallback\n" + ("x" * (TRANSCRIPT_FALLBACK_MAX_CHARS + 100))

        with (
            patch("gobby.sessions.summarize._resolve_provider", return_value=provider),
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.sessions.transcripts.claude.ClaudeTranscriptParser") as MockParser,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.workflows.summary_actions._format_structured_context",
                return_value="structured",
            ),
            patch(
                "gobby.workflows.summary_actions.format_turns_for_llm",
                return_value=formatted,
            ) as mock_format,
        ):
            MockPromptLoader.return_value.load.return_value.content = "prompt"
            MockParser.return_value.extract_turns_since_clear.return_value = turns
            MockParser.return_value.extract_last_messages.return_value = []

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=turns,
                handoff_ctx=handoff_ctx,
                llm_service=None,
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown == "# Transcript Summary"
        assert full_error is None
        formatted_turns = mock_format.call_args.args[0]
        assert len(formatted_turns) == TRANSCRIPT_FALLBACK_MAX_TURNS
        context = provider.generate_summary.await_args.args[0]
        assert len(context["transcript_summary"]) <= TRANSCRIPT_FALLBACK_MAX_CHARS + 4
        assert context["transcript_summary"].endswith("...")

    @pytest.mark.asyncio
    async def test_invalid_provider_summary_returns_generic_error(self) -> None:
        session = _make_session(
            session_id="sess-invalid",
            transcript_path="/tmp/transcript.jsonl",
            digest_markdown="### Turn 1\nDigest source.",
        )
        handoff_ctx = MagicMock()
        handoff_ctx.git_status = ""
        session_manager = MagicMock()
        session_manager.db = None
        provider = AsyncMock()
        provider.generate_summary.return_value = "Session summary generation failed: provider down"

        with (
            patch("gobby.sessions.summarize._resolve_provider", return_value=provider),
            patch("gobby.prompts.loader.PromptLoader") as MockPromptLoader,
            patch("gobby.workflows.git_utils.get_file_changes", return_value=[]),
            patch("gobby.workflows.git_utils.get_git_diff_summary", return_value=""),
            patch(
                "gobby.workflows.summary_actions._format_structured_context",
                return_value="structured",
            ),
        ):
            MockPromptLoader.return_value.load.return_value.content = "prompt"

            full_markdown, full_error = await _generate_full_summary(
                session=session,
                turns=[],
                handoff_ctx=handoff_ctx,
                llm_service=None,
                db=None,
                session_manager=session_manager,
            )

        assert full_markdown is None
        assert full_error == "Generated session summary was invalid"

    @pytest.mark.asyncio
    async def test_provider_failure_string_is_not_persisted(self, tmp_path: Path) -> None:
        transcript_path = _write_transcript(tmp_path)
        sm = MagicMock()
        sm.get.return_value = _make_session(transcript_path=transcript_path)

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=("Session summary generation failed: provider unavailable", None),
            ),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="# Fallback Summary",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                set_handoff_ready=False,
            )

        assert result["success"] is True
        sm.update_summary.assert_called_once_with(
            "sess-1",
            summary_markdown="# Fallback Summary",
        )
        assert sm.update_summary.call_count == 1
        assert sm.update_summary.call_args is not None

    @pytest.mark.asyncio
    async def test_deterministic_fallback_persists_latest_turn_when_digest_lags(self) -> None:
        sm = MagicMock()
        sm.get.return_value = _make_session(
            digest_markdown="### Turn 1\nOld compact handoff.",
            last_turn_markdown="Fresh compact handoff: #14653 is needs_review.",
        )

        with (
            patch("gobby.sessions.summarize._enrich_git_context"),
            patch(
                "gobby.sessions.summarize._generate_full_summary",
                return_value=(None, "provider unavailable"),
            ),
            patch(
                "gobby.sessions.formatting.format_handoff_as_markdown",
                return_value="# Fallback Summary",
            ),
        ):
            result = await generate_session_summaries(
                session_id="sess-1",
                session_manager=sm,
                set_handoff_ready=False,
            )

        assert result["success"] is True
        persisted = sm.update_summary.call_args.kwargs["summary_markdown"]
        assert "Old compact handoff." in persisted
        assert "Fresh compact handoff: #14653 is needs_review." in persisted


class TestGetClaimedTasks:
    """Tests for _get_claimed_tasks()."""

    def _task_state_defaults(self, task: MagicMock, state: str) -> None:
        task.closed_at = None
        task.escalated_at = None
        task.is_escalated = False
        task.current_stage = {"state": state}

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
        self._task_state_defaults(mock_task, "in_progress")
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

    def test_formats_task_without_seq_num(self) -> None:
        """Tasks without seq_num use truncated ID as ref."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_task = MagicMock()
        mock_task.id = "task-uuid-1234-full"
        mock_task.seq_num = None
        self._task_state_defaults(mock_task, "ready")
        mock_task.title = "No seq num task"
        mock_task.description = None

        mock_db = MagicMock()
        with (
            patch("gobby.storage.session_tasks.SessionTaskManager") as MockSTM,
            patch("gobby.storage.task_dependencies.TaskDependencyManager") as MockDep,
        ):
            MockSTM.return_value.get_session_tasks.return_value = [{"task": mock_task}]
            MockDep.return_value.get_all_dependencies.return_value = []
            result = _get_claimed_tasks("sess-1", mock_db)

        assert "task-uui" in result
        assert "[ready]" in result

    def test_formats_task_with_blockers(self) -> None:
        """Tasks with blocking dependencies show blocker info."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_task = MagicMock()
        mock_task.id = "task-uuid-1234"
        mock_task.seq_num = 5
        self._task_state_defaults(mock_task, "ready")
        mock_task.title = "Blocked task"
        mock_task.description = None

        mock_dep = MagicMock()
        mock_dep.dep_type = "blocks"
        mock_dep.depends_on = "blocker-id-xyz"

        mock_db = MagicMock()
        with (
            patch("gobby.storage.session_tasks.SessionTaskManager") as MockSTM,
            patch("gobby.storage.task_dependencies.TaskDependencyManager") as MockDep,
        ):
            MockSTM.return_value.get_session_tasks.return_value = [{"task": mock_task}]
            MockDep.return_value.get_all_dependencies.return_value = [mock_dep]
            result = _get_claimed_tasks("sess-1", mock_db)

        assert "Blocked by:" in result
        assert "blocker-" in result

    def test_long_description_truncated(self) -> None:
        """Descriptions longer than 120 chars are truncated."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_task = MagicMock()
        mock_task.id = "task-uuid-1234"
        mock_task.seq_num = 1
        self._task_state_defaults(mock_task, "ready")
        mock_task.title = "Long desc task"
        mock_task.description = "A" * 200

        mock_db = MagicMock()
        with (
            patch("gobby.storage.session_tasks.SessionTaskManager") as MockSTM,
            patch("gobby.storage.task_dependencies.TaskDependencyManager") as MockDep,
        ):
            MockSTM.return_value.get_session_tasks.return_value = [{"task": mock_task}]
            MockDep.return_value.get_all_dependencies.return_value = []
            result = _get_claimed_tasks("sess-1", mock_db)

        assert "..." in result
        assert "Long desc task" in result

    def test_exception_returns_empty(self) -> None:
        """Exception during task lookup returns empty string."""
        from gobby.sessions.summarize import _get_claimed_tasks

        mock_db = MagicMock()
        with patch(
            "gobby.storage.session_tasks.SessionTaskManager", side_effect=RuntimeError("fail")
        ):
            result = _get_claimed_tasks("sess-1", mock_db)
        assert result == ""


class TestGetSessionMemories:
    """Tests for _get_session_memories()."""

    def test_returns_empty_on_no_memories(self) -> None:
        from gobby.sessions.summarize import _get_session_memories

        mock_db = MagicMock()
        mock_db.fetchall.return_value = []
        result = _get_session_memories("sess-1", mock_db)
        assert result == ""

    def test_formats_memories(self) -> None:
        from gobby.sessions.summarize import _get_session_memories

        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {"content": "Remember this fact", "tags": '["tag1", "tag2"]', "memory_type": "fact"},
        ]
        result = _get_session_memories("sess-1", mock_db)
        assert "[fact]" in result
        assert "Remember this fact" in result
        assert "tag1, tag2" in result

    def test_truncates_long_content(self) -> None:
        from gobby.sessions.summarize import _get_session_memories

        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {"content": "X" * 300, "tags": None, "memory_type": None},
        ]
        result = _get_session_memories("sess-1", mock_db)
        assert "..." in result
        assert "[fact]" in result  # default memory_type

    def test_invalid_tags_json_kept_as_string(self) -> None:
        from gobby.sessions.summarize import _get_session_memories

        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {"content": "data", "tags": "not-json", "memory_type": "note"},
        ]
        result = _get_session_memories("sess-1", mock_db)
        assert "not-json" in result

    def test_exception_returns_empty(self) -> None:
        from gobby.sessions.summarize import _get_session_memories

        mock_db = MagicMock()
        mock_db.fetchall.side_effect = RuntimeError("db error")
        result = _get_session_memories("sess-1", mock_db)
        assert result == ""


class TestExtractDigestTurns:
    """Tests for _extract_digest_turns()."""

    def test_none_input(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        first, recent = _extract_digest_turns(None)
        assert first == ""
        assert recent == ""

    def test_empty_string(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        first, recent = _extract_digest_turns("")
        assert first == ""
        assert recent == ""

    def test_no_turn_structure(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        text = "Just some text without turn headers. " * 20
        first, recent = _extract_digest_turns(text)
        assert len(first) <= 500
        assert recent == ""

    def test_single_turn(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        text = "### Turn 1\nDid some work."
        first, recent = _extract_digest_turns(text)
        assert "Turn 1" in first
        assert "Did some work" in first

    def test_multiple_turns(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        text = (
            "### Turn 1\nFirst turn content.\n"
            "### Turn 2\nSecond turn content.\n"
            "### Turn 3\nThird turn content.\n"
        )
        first, recent = _extract_digest_turns(text)
        assert "Turn 1" in first
        assert "Turn 2" in recent or "Turn 3" in recent

    def test_truncation_on_long_turns(self) -> None:
        from gobby.sessions.summarize import _extract_digest_turns

        long_content = "X" * 2000
        text = f"### Turn 1\n{long_content}\n### Turn 2\n{long_content}\n"
        first, recent = _extract_digest_turns(text)
        assert len(first) <= 810  # 800 + up to 10 chars for "..." suffix
        assert len(recent) <= 1510


class TestReadTranscript:
    """Tests for _read_transcript()."""

    @pytest.mark.asyncio
    async def test_reads_valid_jsonl(self, tmp_path: Path) -> None:
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({"type": "human", "content": "hello"}),
            json.dumps({"type": "assistant", "content": "hi"}),
        ]
        path.write_text("\n".join(lines))
        turns = await _read_transcript(path)
        assert len(turns) == 2

    @pytest.mark.asyncio
    async def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({"type": "human"}),
            "not valid json{{{",
            json.dumps({"type": "assistant"}),
        ]
        path.write_text("\n".join(lines))
        turns = await _read_transcript(path)
        assert len(turns) == 2

    @pytest.mark.asyncio
    async def test_skips_empty_lines(self, tmp_path: Path) -> None:
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "transcript.jsonl"
        path.write_text(json.dumps({"type": "human"}) + "\n\n\n")
        turns = await _read_transcript(path)
        assert len(turns) == 1

    @pytest.mark.asyncio
    async def test_skips_non_dict_json_values(self, tmp_path: Path) -> None:
        """Non-dict JSON values (bare strings, numbers) are filtered out."""
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "transcript.jsonl"
        lines = [
            json.dumps({"type": "assistant"}),
            json.dumps("bare string"),
            json.dumps(42),
            json.dumps({"type": "user"}),
        ]
        path.write_text("\n".join(lines))
        turns = await _read_transcript(path)
        assert len(turns) == 2
        assert all(isinstance(t, dict) for t in turns)

    @pytest.mark.asyncio
    async def test_reads_gemini_json_session(self, tmp_path: Path) -> None:
        """Gemini JSON session files are parsed and messages extracted."""
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "session.json"
        session_data = {
            "sessionId": "test-session",
            "messages": [
                {
                    "id": "msg-1",
                    "type": "user",
                    "timestamp": "2026-04-12T16:20:00Z",
                    "content": [{"text": "Hello there"}],
                },
                {
                    "id": "msg-2",
                    "type": "gemini",
                    "timestamp": "2026-04-12T16:20:01Z",
                    "content": "Hi! How can I help?",
                    "toolCalls": [],
                },
            ],
            "kind": "main",
        }
        path.write_text(json.dumps(session_data))
        turns = await _read_transcript(path, source="gemini")
        assert len(turns) == 2
        assert turns[0]["type"] == "user"
        assert turns[1]["type"] == "gemini"
        assert turns[1]["content"] == "Hi! How can I help?"

    @pytest.mark.asyncio
    async def test_gemini_json_invalid_json(self, tmp_path: Path) -> None:
        """Invalid JSON in Gemini file returns empty list."""
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "session.json"
        path.write_text("not valid json{{{")
        turns = await _read_transcript(path, source="gemini")
        assert turns == []

    @pytest.mark.asyncio
    async def test_gemini_json_no_messages_key(self, tmp_path: Path) -> None:
        """Gemini JSON without messages key returns empty list."""
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "session.json"
        path.write_text(json.dumps({"sessionId": "test", "kind": "main"}))
        turns = await _read_transcript(path, source="gemini")
        assert turns == []

    @pytest.mark.asyncio
    async def test_non_gemini_json_falls_through_to_jsonl(self, tmp_path: Path) -> None:
        """A .json file with source='claude' is still treated as JSONL."""
        from gobby.sessions.summarize import _read_transcript

        path = tmp_path / "transcript.json"
        lines = [
            json.dumps({"type": "user", "message": {"content": "hello"}}),
            json.dumps({"type": "assistant", "message": {"content": "hi"}}),
        ]
        path.write_text("\n".join(lines))
        turns = await _read_transcript(path, source="claude")
        assert len(turns) == 2


class TestWriteFiles:
    """Tests for _write_files()."""

    @pytest.mark.asyncio
    async def test_no_write_when_disabled(self) -> None:
        from gobby.sessions.summarize import _write_files

        sm = MagicMock()
        result = await _write_files(
            session_id="s1",
            full_markdown="# Full",
            write_file=False,
            output_path="~/.gobby/summaries",
            session_manager=sm,
        )
        assert result == []
