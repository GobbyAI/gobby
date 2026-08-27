"""Tests for session summary generation and file persistence."""

import json
import logging
import threading
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.sessions import SessionSummaryConfig
from gobby.hooks.tool_error_tracker import (
    load_open_tool_errors,
    normalize_open_tool_error_records,
)
from gobby.sessions.analyzer import HandoffContext
from gobby.sessions.summary_formatting import (
    _format_structured_context,
    format_unresolved_errors,
)
from gobby.sessions.summary_generation import _write_summary_file, generate_summary
from gobby.sessions.summary_transcripts import (
    TRANSCRIPT_FALLBACK_MAX_CHARS,
    _truncate_markdown,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _local_session_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gobby.sessions.summary_generation.require_local_session_ownership",
        lambda _session: "local-machine",
    )


VALID_SUMMARY_CONTENT = """# Session Summary

## Current State

The implementation is complete and the focused workflow tests pass with the expected behavior.

## Next Steps

Continue with the remaining task validation and lifecycle handoff steps.
"""


def _valid_summary_template(body: str) -> str:
    return f"{body}\n\n## Current State\n\n## Next Steps"


def test_long_error_referenced_not_cut() -> None:
    timestamp = "2026-07-23T12:00:00+00:00"
    path_list = " | ".join(f"src/package_{index}/validator.py" for index in range(24))
    error = f"validator rejected paths: {path_list}".ljust(900, "!")
    records = normalize_open_tool_error_records(
        [
            {
                "tool": "gobby-tasks:close_task",
                "target_key": "task:#19338",
                "error": error,
                "first_at": timestamp,
                "last_at": timestamp,
                "count": 1,
            }
        ]
    )

    rendered = _format_structured_context(HandoffContext(unresolved_errors=records))
    error_id = records[0]["error_id"]

    assert error not in rendered
    assert 'get_variable(name="open_tool_errors", session_id=<current>)' in rendered
    assert f'error_id="{error_id}"' in rendered
    assert {record["error_id"]: record["error"] for record in records}[error_id] == error

    overflow = _truncate_markdown(error * 30, 500)
    assert len(overflow) <= 500
    assert "get_handoff_context (gobby-sessions)" in overflow
    assert overflow.endswith("... [truncated]")
    assert _truncate_markdown(error, 16) == "get_handoff_cont"


@pytest.mark.parametrize(
    "digest_markdown",
    ["d" * TRANSCRIPT_FALLBACK_MAX_CHARS, None],
    ids=["digest", "analyzer"],
)
@pytest.mark.parametrize("record_count", [10, 200], ids=["normal-errors", "errors-exceed-cap"])
@pytest.mark.asyncio
async def test_generate_summary_reserves_unresolved_errors_within_context_cap(
    digest_markdown: str | None,
    record_count: int,
    mock_session_manager: MagicMock,
    mock_llm_service: MagicMock,
    mock_transcript_processor: MagicMock,
    summary_config: SessionSummaryConfig,
    tmp_path: Path,
) -> None:
    transcript_file = tmp_path / "transcript.jsonl"
    transcript_file.write_text(
        json.dumps({"message": {"role": "user", "content": "Help me"}}) + "\n"
    )
    session = MagicMock(
        transcript_path=str(transcript_file),
        source="claude",
        digest_markdown=digest_markdown,
        terminal_context={"cwd": str(tmp_path)},
    )
    mock_session_manager.get.return_value = session
    mock_session_manager.db = MagicMock()
    mock_transcript_processor.extract_turns_since_clear.return_value = []
    mock_transcript_processor.extract_last_messages.return_value = []
    records = [
        {
            "tool": f"{index:02d}" + ("t" * 128),
            "target_key": f"{index:02d}" + ("k" * 128),
            "error": "e" * 300,
            "first_at": "2026-07-23T00:00:00+00:00",
            "last_at": "2026-07-23T00:00:01+00:00",
            "count": 999_999,
        }
        for index in range(record_count)
    ]
    main_thread = threading.get_ident()
    loader_threads: list[int] = []
    captured_context: dict[str, Any] = {}

    def load_records(_db: object, _session_id: str) -> list[dict[str, Any]]:
        loader_threads.append(threading.get_ident())
        return records

    def capture_prompt(_template: str, context: dict[str, Any]) -> str:
        captured_context.update(context)
        return "rendered prompt"

    with (
        patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
        patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
        patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        patch("gobby.sessions.summary_generation.get_recent_git_commits", return_value=[]),
        patch(
            "gobby.sessions.summary_generation._format_structured_context",
            return_value="a" * TRANSCRIPT_FALLBACK_MAX_CHARS,
        ),
        patch(
            "gobby.sessions.summary_generation.load_open_tool_errors",
            side_effect=load_records,
        ),
        patch("gobby.llm.prompt_rendering.render_summary_prompt", side_effect=capture_prompt),
    ):
        result = await generate_summary(
            session_manager=mock_session_manager,
            session_id="test-session",
            llm_service=mock_llm_service,
            transcript_processor=mock_transcript_processor,
            session_summary_config=summary_config,
            template=_valid_summary_template("{structured_context}"),
        )

    assert result is not None
    assert result["summary_generated"] is True
    structured_context = captured_context["structured_context"]
    expected_block = format_unresolved_errors(records)
    assert len(structured_context) <= TRANSCRIPT_FALLBACK_MAX_CHARS
    if len(expected_block) > TRANSCRIPT_FALLBACK_MAX_CHARS:
        assert structured_context.startswith("Unresolved Tool Errors:")
        assert structured_context.endswith("... [truncated]")
        assert "a" * 100 not in structured_context
    else:
        assert structured_context.endswith(expected_block)
    assert loader_threads and len(loader_threads) == 1
    assert loader_threads[0] != main_thread


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_session_manager() -> MagicMock:
    """Create a mock session manager."""
    manager = MagicMock()
    return manager


@pytest.fixture
def mock_llm_service() -> MagicMock:
    """Create a mock LLM service."""
    service = MagicMock()
    service.call_feature = AsyncMock(return_value=VALID_SUMMARY_CONTENT)
    return service


@pytest.fixture
def summary_config() -> SessionSummaryConfig:
    return SessionSummaryConfig(candidates=["claude/haiku"])


@pytest.fixture
def mock_transcript_processor() -> MagicMock:
    """Create a mock transcript processor."""
    processor = MagicMock()
    processor.extract_turns_since_clear.return_value = []
    processor.extract_last_messages.return_value = []
    return processor


@pytest.fixture
def mock_template_engine() -> MagicMock:
    """Create a mock template engine."""
    engine = MagicMock()
    engine.render.side_effect = lambda template, context: template.replace(
        "{{ transcript }}", context.get("transcript", "")
    )
    return engine


@pytest.fixture
def sample_transcript_file(tmp_path: Path) -> Path:
    """Create a sample transcript JSONL file."""
    transcript_file = tmp_path / "transcript.jsonl"
    turns = [
        {"message": {"role": "user", "content": "Hello, can you help me?"}},
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Of course! How can I assist you today?"}],
            }
        },
        {"message": {"role": "user", "content": "I need to refactor some code."}},
    ]
    with open(transcript_file, "w") as f:
        for turn in turns:
            f.write(json.dumps(turn) + "\n")
    return transcript_file


@pytest.fixture
def mock_session(tmp_path: Path) -> MagicMock:
    """Create a mock session object with transcript path."""
    session = MagicMock()
    transcript_file = tmp_path / "transcript.jsonl"
    # Create a basic transcript
    with open(transcript_file, "w") as f:
        f.write(json.dumps({"message": {"role": "user", "content": "test"}}) + "\n")
    session.transcript_path = str(transcript_file)
    return session


class TestGenerateSummary:
    """Tests for the generate_summary async function."""

    @pytest.mark.asyncio
    async def test_generate_summary_success(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test successful summary generation."""
        transcript_file = tmp_path / "transcript.jsonl"
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "Help me"}}) + "\n")

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session

        mock_transcript_processor.extract_turns_since_clear.return_value = [
            {"message": {"role": "user", "content": "Help me"}}
        ]
        mock_transcript_processor.extract_last_messages.return_value = []

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
            caplog.at_level(logging.DEBUG, logger="gobby.sessions.summary_generation"),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
            )

        assert result is not None
        assert result["summary_generated"] is True
        assert result["summary_length"] == len(VALID_SUMMARY_CONTENT)
        mock_session_manager.update_summary.assert_called_once()
        generation_record = next(
            record
            for record in caplog.records
            if record.getMessage().startswith("Generated summary for session ")
        )
        assert generation_record.levelno == logging.DEBUG
        assert not any(
            record.levelno == logging.INFO
            and record.getMessage().startswith("Generated summary for session ")
            for record in caplog.records
        )
        assert generation_record.getMessage() == "Generated summary for session test-session"
        assert generation_record.__dict__["mode"] == "clear"
        assert generation_record.__dict__["reason"] == "workflow_action"
        assert generation_record.__dict__["output_chars"] == len(VALID_SUMMARY_CONTENT)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("suffix", "source", "transcript_content"),
        [
            (
                ".jsonl",
                "claude",
                json.dumps({"message": {"role": "user", "content": "Help me"}}) + "\n{torn",
            ),
            (
                ".json",
                "qwen",
                json.dumps(
                    {
                        "messages": [
                            {"type": "user", "content": [{"text": "Help me"}]},
                        ]
                    }
                ),
            ),
            (
                ".json",
                "unknown",
                json.dumps(
                    {
                        "messages": [
                            {"type": "gemini", "content": "I can help"},
                        ]
                    }
                ),
            ),
        ],
    )
    async def test_generate_summary_reads_supported_transcript_formats(
        self,
        suffix: str,
        source: str,
        transcript_content: str,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        transcript_file = tmp_path / f"transcript{suffix}"
        transcript_file.write_text(transcript_content)
        session = MagicMock(
            transcript_path=str(transcript_file),
            source=source,
            digest_markdown="Existing digest",
            terminal_context={"cwd": str(tmp_path)},
        )
        mock_session_manager.get.return_value = session
        mock_transcript_processor.extract_turns_since_clear.side_effect = (
            lambda turns, max_turns: turns[-max_turns:]
        )
        mock_transcript_processor.extract_last_messages.return_value = []

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
            patch("gobby.sessions.summary_generation.get_recent_git_commits", return_value=[]),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
            )

        assert result is not None
        assert result["summary_generated"] is True
        parsed_turns = mock_transcript_processor.extract_turns_since_clear.call_args.args[0]
        assert len(parsed_turns) == 1

    @pytest.mark.asyncio
    async def test_generate_summary_offloads_io_and_uses_session_workspace(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        transcript_file = tmp_path / "transcript.jsonl"
        transcript_file.write_text(json.dumps({"message": {"role": "user", "content": "Help me"}}))
        session = MagicMock(
            transcript_path=str(transcript_file),
            source="claude",
            digest_markdown="Existing digest",
            terminal_context={"cwd": str(tmp_path)},
        )
        mock_session_manager.get.return_value = session
        mock_transcript_processor.extract_turns_since_clear.return_value = []
        mock_transcript_processor.extract_last_messages.return_value = []

        async def run_in_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        with (
            patch(
                "gobby.sessions.summary_generation.asyncio.to_thread",
                new=AsyncMock(side_effect=run_in_thread),
            ) as to_thread,
            patch(
                "gobby.sessions.summary_generation.get_git_status", return_value="clean"
            ) as status,
            patch(
                "gobby.sessions.summary_generation.get_file_changes", return_value="No changes"
            ) as changes,
            patch(
                "gobby.sessions.summary_generation.get_git_diff_summary", return_value=""
            ) as diff,
            patch(
                "gobby.sessions.summary_generation.get_recent_git_commits", return_value=[]
            ) as commits,
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
            )

        assert result is not None
        assert result["summary_generated"] is True
        assert to_thread.await_count == 9
        to_thread.assert_any_await(
            load_open_tool_errors,
            mock_session_manager.db,
            "test-session",
        )
        status.assert_called_once_with(str(tmp_path))
        changes.assert_called_once_with(str(tmp_path))
        diff.assert_called_once_with(8000, str(tmp_path))
        commits.assert_called_once_with(10, str(tmp_path))

    @pytest.mark.asyncio
    async def test_generate_summary_caps_prompt_inputs(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        transcript_file = tmp_path / "transcript.jsonl"
        transcript_file.write_text(
            json.dumps({"message": {"role": "user", "content": "t" * 30_000}})
        )
        session = MagicMock(
            transcript_path=str(transcript_file),
            source="claude",
            digest_markdown="d" * 30_000,
            terminal_context={"cwd": str(tmp_path)},
        )
        mock_session_manager.get.return_value = session
        mock_transcript_processor.extract_turns_since_clear.side_effect = (
            lambda turns, max_turns: turns[-max_turns:]
        )
        mock_transcript_processor.extract_last_messages.return_value = []

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
            patch("gobby.sessions.summary_generation.get_recent_git_commits", return_value=[]),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
                template=_valid_summary_template("{transcript_summary}|{structured_context}"),
            )

        assert result is not None
        assert result["summary_generated"] is True
        transcript_context, structured_context = mock_llm_service.call_feature.await_args.args[
            1
        ].split("|", maxsplit=1)
        assert len(transcript_context) <= TRANSCRIPT_FALLBACK_MAX_CHARS
        assert transcript_context.endswith("... [truncated]")
        bounded_structured_context = structured_context.split(
            "\n\n## Current State",
            maxsplit=1,
        )[0]
        assert len(bounded_structured_context) <= TRANSCRIPT_FALLBACK_MAX_CHARS
        assert bounded_structured_context.endswith("... [truncated]")

    @pytest.mark.parametrize("llm_output", ["", "   \n"])
    async def test_generate_summary_rejects_invalid_llm_output(
        self,
        llm_output: str,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Invalid LLM output must not replace an existing summary."""
        transcript_file = tmp_path / "transcript.jsonl"
        transcript_file.write_text(
            json.dumps({"message": {"role": "user", "content": "Help me"}}) + "\n"
        )

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        session.summary_markdown = "Existing good summary"
        mock_session_manager.get.return_value = session
        mock_llm_service.call_feature.return_value = llm_output

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
            )

        assert result == {
            "error": "LLM returned invalid summary: summary is shorter than 100 characters"
        }
        mock_session_manager.update_summary.assert_not_called()
        assert session.summary_markdown == "Existing good summary"

    @pytest.mark.asyncio
    async def test_generate_summary_invalid_mode(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
    ) -> None:
        """Test that invalid mode raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
                mode=cast(Any, "invalid_mode"),
            )

        assert "Invalid mode 'invalid_mode'" in str(exc_info.value)
        assert "clear" in str(exc_info.value)
        assert "compact" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_generate_summary_clear_mode(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Test summary generation in clear mode."""
        transcript_file = tmp_path / "transcript.jsonl"
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n")

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session

        mock_transcript_processor.extract_turns_since_clear.return_value = [
            {"message": {"role": "user", "content": "Test"}}
        ]
        mock_transcript_processor.extract_last_messages.return_value = []

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
                template=_valid_summary_template("Mode: {mode}\nTranscript:\n{transcript_summary}"),
                mode="clear",
                write_file=True,
                output_path=str(tmp_path),
            )

        assert result is not None
        assert result["summary_generated"] is True
        assert result["summary_file"] == str(tmp_path / "test-session-clear.md")
        prompt = mock_llm_service.call_feature.await_args.args[1]
        assert "Mode: clear" in prompt

    @pytest.mark.asyncio
    async def test_generate_summary_compact_mode(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Test summary generation in compact mode."""
        transcript_file = tmp_path / "transcript.jsonl"
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n")

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session

        mock_transcript_processor.extract_turns_since_clear.return_value = [
            {"message": {"role": "user", "content": "Test"}}
        ]
        mock_transcript_processor.extract_last_messages.return_value = []

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
                template=_valid_summary_template("Mode: {mode}\nTranscript:\n{transcript_summary}"),
                mode="compact",
                write_file=True,
                output_path=str(tmp_path),
            )

        assert result is not None
        assert result["summary_generated"] is True
        assert result["summary_file"] == str(tmp_path / "test-session-compact.md")
        prompt = mock_llm_service.call_feature.await_args.args[1]
        assert "Mode: compact" in prompt

    @pytest.mark.asyncio
    async def test_generate_summary_with_previous_summary(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Test summary generation with previous summary for cumulative compression."""
        transcript_file = tmp_path / "transcript.jsonl"
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n")

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session

        mock_transcript_processor.extract_turns_since_clear.return_value = []
        mock_transcript_processor.extract_last_messages.return_value = []

        previous = "Previous session summary content"

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
                template=_valid_summary_template("Previous:\n{previous_summary}\nMode: {mode}"),
                previous_summary=previous,
                mode="compact",
            )

        assert result is not None
        assert result["summary_generated"] is True
        prompt = mock_llm_service.call_feature.await_args.args[1]
        assert previous in prompt

    @pytest.mark.asyncio
    async def test_generate_summary_missing_services(
        self,
        mock_session_manager: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
    ) -> None:
        """Test summary generation with missing LLM service."""
        result = await generate_summary(
            session_manager=mock_session_manager,
            session_id="test-session",
            llm_service=None,
            transcript_processor=mock_transcript_processor,
            session_summary_config=summary_config,
        )

        assert result == {"error": "Missing services"}

    @pytest.mark.asyncio
    async def test_generate_summary_missing_transcript_processor(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        summary_config: SessionSummaryConfig,
    ) -> None:
        """Test summary generation with missing transcript processor."""
        result = await generate_summary(
            session_manager=mock_session_manager,
            session_id="test-session",
            llm_service=mock_llm_service,
            transcript_processor=None,
            session_summary_config=summary_config,
        )

        assert result == {"error": "Missing services"}

    @pytest.mark.asyncio
    async def test_generate_summary_session_not_found(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
    ) -> None:
        """Test summary generation when session is not found."""
        mock_session_manager.get.return_value = None

        result = await generate_summary(
            session_manager=mock_session_manager,
            session_id="nonexistent",
            llm_service=mock_llm_service,
            transcript_processor=mock_transcript_processor,
            session_summary_config=summary_config,
        )

        assert result == {"error": "Session not found"}

    @pytest.mark.asyncio
    async def test_generate_summary_no_transcript_path(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
    ) -> None:
        """Test summary generation when session has no transcript path."""
        session = MagicMock()
        session.transcript_path = None
        mock_session_manager.get.return_value = session

        result = await generate_summary(
            session_manager=mock_session_manager,
            session_id="test-session",
            llm_service=mock_llm_service,
            transcript_processor=mock_transcript_processor,
            session_summary_config=summary_config,
        )

        assert result == {"error": "No transcript path"}

    @pytest.mark.asyncio
    async def test_generate_summary_transcript_not_found(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Test summary generation when transcript file doesn't exist."""
        session = MagicMock()
        session.transcript_path = str(tmp_path / "nonexistent.jsonl")
        mock_session_manager.get.return_value = session

        result = await generate_summary(
            session_manager=mock_session_manager,
            session_id="test-session",
            llm_service=mock_llm_service,
            transcript_processor=mock_transcript_processor,
            session_summary_config=summary_config,
        )

        assert result == {"error": "Transcript not found"}

    @pytest.mark.asyncio
    async def test_generate_summary_reports_malformed_transcript_lines(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Durable malformed JSONL records abort summary generation."""
        transcript_file = tmp_path / "bad_transcript.jsonl"
        with open(transcript_file, "w") as f:
            f.write("invalid json content\n")

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session

        result = await generate_summary(
            session_manager=mock_session_manager,
            session_id="test-session",
            llm_service=mock_llm_service,
            transcript_processor=mock_transcript_processor,
            session_summary_config=summary_config,
        )

        assert result is not None
        assert "Corrupt transcript record" in result["error"]

    @pytest.mark.asyncio
    async def test_generate_summary_llm_error(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Test summary generation when LLM call fails."""
        transcript_file = tmp_path / "transcript.jsonl"
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n")

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session

        mock_transcript_processor.extract_turns_since_clear.return_value = []
        mock_transcript_processor.extract_last_messages.return_value = []

        mock_llm_service.call_feature.side_effect = Exception("LLM API Error")

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
            )

        assert result is not None
        assert "error" in result
        assert "LLM error" in result["error"]

    @pytest.mark.asyncio
    async def test_generate_summary_with_custom_template(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Test summary generation with custom template."""
        transcript_file = tmp_path / "transcript.jsonl"
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n")

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session

        mock_transcript_processor.extract_turns_since_clear.return_value = []
        mock_transcript_processor.extract_last_messages.return_value = []

        custom_template = _valid_summary_template("Custom summary template: {transcript_summary}")

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
                template=custom_template,
            )

        assert result is not None
        assert result["summary_generated"] is True
        assert mock_llm_service.call_feature.await_args.args[0] is summary_config
        prompt = mock_llm_service.call_feature.await_args.args[1]
        assert prompt.startswith("Custom summary template:")

    @pytest.mark.asyncio
    async def test_generate_summary_rejects_invalid_custom_prompt_before_llm_call(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
    ) -> None:
        session = MagicMock()
        session.transcript_path = "/tmp/unused-transcript.jsonl"
        mock_session_manager.get.return_value = session

        result = await generate_summary(
            session_manager=mock_session_manager,
            session_id="test-session",
            llm_service=mock_llm_service,
            transcript_processor=mock_transcript_processor,
            session_summary_config=summary_config,
            template="Custom prompt without required headings",
        )

        assert result == {
            "error": (
                "Invalid summary prompt template: summary prompt must include literal "
                "required heading(s): ## Current State, ## Next Steps"
            )
        }
        mock_llm_service.call_feature.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generate_summary_rejects_invalid_config_prompt_before_llm_call(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
    ) -> None:
        session = MagicMock()
        session.transcript_path = "/tmp/unused-transcript.jsonl"
        mock_session_manager.get.return_value = session
        config = SessionSummaryConfig(
            prompt="Configured prompt without required headings",
            candidates=["claude/haiku"],
        )

        result = await generate_summary(
            session_manager=mock_session_manager,
            session_id="test-session",
            llm_service=mock_llm_service,
            transcript_processor=mock_transcript_processor,
            session_summary_config=config,
        )

        assert result == {
            "error": (
                "Invalid summary prompt template: summary prompt must include literal "
                "required heading(s): ## Current State, ## Next Steps"
            )
        }
        mock_llm_service.call_feature.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generate_summary_prefers_installed_prompt_over_config_fallback(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        tmp_path: Path,
    ) -> None:
        transcript_file = tmp_path / "transcript.jsonl"
        transcript_file.write_text(
            json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n"
        )
        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session
        config = SessionSummaryConfig(
            prompt=_valid_summary_template("Configured: {transcript_summary}"),
            candidates=["claude/haiku"],
        )
        installed_prompt = _valid_summary_template("Installed: {transcript_summary}")

        with (
            patch(
                "gobby.sessions.summary_generation.load_summary_prompt_template",
                return_value=installed_prompt,
            ),
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=config,
            )

        assert result is not None
        assert result is not None
        assert result["summary_generated"] is True
        prompt = mock_llm_service.call_feature.await_args.args[1]
        assert prompt.startswith("Installed:")
        assert "Configured:" not in prompt

    @pytest.mark.asyncio
    async def test_generate_summary_uses_secondary_candidate_after_validation_rejection(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        transcript_file = tmp_path / "transcript.jsonl"
        transcript_file.write_text(
            json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n"
        )
        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session
        attempted: list[str] = []

        async def call_feature(*_args: object, **kwargs: object) -> str:
            output_validator = kwargs.get("output_validator")
            assert callable(output_validator)
            for output in (
                "Detailed prose without the mandatory semantic sections. " * 4,
                VALID_SUMMARY_CONTENT,
            ):
                attempted.append(output)
                if output_validator(output) is None:
                    return output
            raise AssertionError("expected secondary candidate to validate")

        mock_llm_service.call_feature.side_effect = call_feature

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
                template=_valid_summary_template("Transcript: {transcript_summary}"),
            )

        assert result is not None
        assert result["summary_generated"] is True
        assert attempted == [
            "Detailed prose without the mandatory semantic sections. " * 4,
            VALID_SUMMARY_CONTENT,
        ]
        mock_session_manager.update_summary.assert_called_once_with(
            "test-session",
            summary_markdown=VALID_SUMMARY_CONTENT,
        )

    @pytest.mark.asyncio
    async def test_generate_summary_includes_git_context(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Test that summary generation includes git status and file changes."""
        transcript_file = tmp_path / "transcript.jsonl"
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n")

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session

        mock_transcript_processor.extract_turns_since_clear.return_value = []
        mock_transcript_processor.extract_last_messages.return_value = []

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="M file.py"),
            patch(
                "gobby.sessions.summary_generation.get_file_changes",
                return_value="Modified/Deleted:\nM\tfile.py",
            ),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
                template=_valid_summary_template("Git:\n{git_status}\nFiles:\n{file_changes}"),
            )

        assert result is not None
        assert result["summary_generated"] is True
        prompt = mock_llm_service.call_feature.await_args.args[1]
        assert "Git:\nM file.py" in prompt
        assert "file.py" in prompt

    @pytest.mark.asyncio
    async def test_generate_summary_uses_session_project_from_different_cwd(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_path = tmp_path / "target"
        project_path.mkdir()
        transcript_file = project_path / "transcript.jsonl"
        transcript_file.write_text(
            json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n"
        )
        other_cwd = tmp_path / "other"
        other_cwd.mkdir()
        monkeypatch.chdir(other_cwd)
        session = MagicMock(
            transcript_path=str(transcript_file),
            terminal_context={"cwd": str(project_path)},
            digest_markdown="Existing digest",
        )
        mock_session_manager.get.return_value = session
        mock_transcript_processor.extract_turns_since_clear.return_value = []
        mock_transcript_processor.extract_last_messages.return_value = []

        with (
            patch(
                "gobby.sessions.summary_generation.get_git_status", return_value="clean"
            ) as status,
            patch(
                "gobby.sessions.summary_generation.get_file_changes", return_value="No changes"
            ) as changes,
            patch(
                "gobby.sessions.summary_generation.get_git_diff_summary", return_value=""
            ) as diff,
            patch(
                "gobby.sessions.summary_generation.get_recent_git_commits", return_value=[]
            ) as commits,
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
            )

        assert result is not None
        assert result["summary_generated"] is True
        status.assert_called_once_with(str(project_path))
        changes.assert_called_once_with(str(project_path))
        diff.assert_called_once_with(8000, str(project_path))
        commits.assert_called_once_with(10, str(project_path))

    @pytest.mark.asyncio
    async def test_generate_summary_includes_last_messages(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Test that summary generation includes last messages in context."""
        transcript_file = tmp_path / "transcript.jsonl"
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n")

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        mock_session_manager.get.return_value = session

        last_messages = [
            {"message": {"role": "user", "content": "Final question"}},
            {"message": {"role": "assistant", "content": "Final answer"}},
        ]
        mock_transcript_processor.extract_turns_since_clear.return_value = []
        mock_transcript_processor.extract_last_messages.return_value = last_messages

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
                template=_valid_summary_template("Messages:\n{last_messages}"),
            )

        assert result is not None
        assert result["summary_generated"] is True
        prompt = mock_llm_service.call_feature.await_args.args[1]
        assert "Final question" in prompt


# =============================================================================
# Tests for _write_summary_file
# =============================================================================


class TestWriteSummaryFile:
    """Tests for the _write_summary_file helper function."""

    @pytest.mark.asyncio
    async def test_write_summary_file_creates_file(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that _write_summary_file creates a summary file."""
        output_dir = str(tmp_path / "session_summaries")

        with caplog.at_level(logging.DEBUG, logger="gobby.sessions.summary_generation"):
            result = await _write_summary_file(
                session_id="test-session-123",
                content="# Test Summary\n\nTest content",
                output_path=output_dir,
            )

        assert result is not None
        from pathlib import Path

        written = Path(result)
        assert written.exists()
        assert written.read_text() == "# Test Summary\n\nTest content"
        write_record = next(
            record for record in caplog.records if record.getMessage() == "Session summary written"
        )
        assert write_record.levelno == logging.DEBUG

    @pytest.mark.asyncio
    async def test_write_summary_file_creates_directory(self, tmp_path: Path) -> None:
        """Test that _write_summary_file creates the output directory."""
        output_dir = str(tmp_path / "nested" / "summaries")

        result = await _write_summary_file(
            session_id="test-session",
            content="Content",
            output_path=output_dir,
        )

        assert result is not None
        from pathlib import Path

        assert Path(output_dir).exists()

    @pytest.mark.asyncio
    async def test_write_summary_file_uses_external_id(self, tmp_path: Path) -> None:
        """Test that _write_summary_file uses external_id in filename."""
        output_dir = str(tmp_path / "summaries")

        mock_manager = MagicMock()
        mock_session = MagicMock()
        mock_session.external_id = "ext-abc-123"
        mock_manager.get.return_value = mock_session

        result = await _write_summary_file(
            session_id="internal-id",
            content="Content",
            output_path=output_dir,
            session_manager=mock_manager,
        )

        assert result is not None
        assert "ext-abc-123" in result
        assert "internal-id" not in result

    @pytest.mark.asyncio
    async def test_write_summary_file_falls_back_to_session_id(self, tmp_path: Path) -> None:
        """Test fallback to session_id when external_id unavailable."""
        output_dir = str(tmp_path / "summaries")

        result = await _write_summary_file(
            session_id="fallback-session-id",
            content="Content",
            output_path=output_dir,
            session_manager=None,
        )

        assert result is not None
        assert "fallback-session-id" in result

    @pytest.mark.asyncio
    async def test_write_summary_file_naming_format(self, tmp_path: Path) -> None:
        """Test that files follow {ref}-{mode}.md format."""
        output_dir = str(tmp_path / "summaries")

        result = await _write_summary_file(
            session_id="my-session",
            content="Content",
            output_path=output_dir,
        )

        assert result is not None
        from pathlib import Path

        filename = Path(result).name
        assert filename == "my-session-clear.md"

    @pytest.mark.asyncio
    async def test_write_summary_file_compact_mode(self, tmp_path: Path) -> None:
        """Test that compact mode uses -compact suffix."""
        output_dir = str(tmp_path / "summaries")

        result = await _write_summary_file(
            session_id="my-session",
            content="Content",
            output_path=output_dir,
            mode="compact",
        )

        assert result is not None
        from pathlib import Path

        filename = Path(result).name
        assert filename == "my-session-compact.md"

    @pytest.mark.asyncio
    async def test_write_summary_file_error_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Test that write errors return None."""
        monkeypatch.setattr(
            "pathlib.Path.mkdir",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("mocked")),
        )

        result = await _write_summary_file(
            session_id="test",
            content="Content",
            output_path=str(tmp_path / "test_summary"),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_generate_summary_with_write_file(
        self,
        mock_session_manager: MagicMock,
        mock_llm_service: MagicMock,
        mock_transcript_processor: MagicMock,
        summary_config: SessionSummaryConfig,
        tmp_path: Path,
    ) -> None:
        """Test generate_summary with write_file=True produces a file."""
        transcript_file = tmp_path / "transcript.jsonl"
        with open(transcript_file, "w") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "Test"}}) + "\n")

        session = MagicMock()
        session.transcript_path = str(transcript_file)
        session.external_id = "ext-write-test"
        mock_session_manager.get.return_value = session

        mock_transcript_processor.extract_turns_since_clear.return_value = []
        mock_transcript_processor.extract_last_messages.return_value = []

        output_dir = str(tmp_path / "write_test_summaries")

        with (
            patch("gobby.sessions.summary_generation.get_git_status", return_value="clean"),
            patch("gobby.sessions.summary_generation.get_file_changes", return_value="No changes"),
            patch("gobby.sessions.summary_generation.get_git_diff_summary", return_value=""),
        ):
            result = await generate_summary(
                session_manager=mock_session_manager,
                session_id="test-session",
                llm_service=mock_llm_service,
                transcript_processor=mock_transcript_processor,
                session_summary_config=summary_config,
                write_file=True,
                output_path=output_dir,
            )

        assert result is not None
        assert result["summary_generated"] is True
        assert "summary_file" in result
        from pathlib import Path

        assert Path(result["summary_file"]).exists()
        assert "ext-write-test" in result["summary_file"]
