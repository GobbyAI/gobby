from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.token_events import TokenEventStore

pytestmark = pytest.mark.unit


def _message(
    *,
    message_id: str,
    input_tokens: int,
    output_tokens: int,
) -> ParsedMessage:
    return ParsedMessage(
        index=0,
        role="assistant",
        content="usage-bearing response",
        content_type="text",
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
        raw_json={"id": message_id},
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        message_id=message_id,
        model=None,
    )


@pytest.mark.asyncio
async def test_non_claude_transcript_events_keep_source_and_session_model_attribution(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    """Codex/Gemini token events should aggregate under their source and model names."""
    project = LocalProjectManager(temp_db).create(
        name="token-attribution-project",
        repo_path=str(tmp_path),
    )
    session_manager = SessionManager(temp_db)
    lifecycle = SessionLifecycleManager(temp_db, SessionLifecycleConfig())

    codex_path = tmp_path / "codex.jsonl"
    codex_path.write_text("{}\n")
    codex_session = session_manager.register(
        external_id="codex-ext",
        machine_id="machine-1",
        source="codex",
        project_id=project.id,
        transcript_path=str(codex_path),
    )
    session_manager.update_usage(
        codex_session.id,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        model="gpt-5-codex",
    )

    # This intentionally exercises the private processor directly so the
    # attribution behavior stays isolated from lifecycle scheduling.
    with patch("gobby.sessions.lifecycle.CodexTranscriptParser") as parser_cls:
        parser_cls.return_value.parse_lines.return_value = [
            _message(message_id="codex-msg", input_tokens=123, output_tokens=45)
        ]
        await lifecycle._process_session_transcript(codex_session.id, str(codex_path))

    gemini_path = tmp_path / "gemini.json"
    gemini_path.write_text('{"sessionId":"gemini-ext","messages":[]}')
    gemini_session = session_manager.register(
        external_id="gemini-ext",
        machine_id="machine-1",
        source="gemini",
        project_id=project.id,
        transcript_path=str(gemini_path),
    )
    session_manager.update_usage(
        gemini_session.id,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        model="gemini-2.5-pro",
    )

    # Keep this direct call paired with the Codex case above for stable
    # source/model attribution coverage.
    with patch("gobby.sessions.lifecycle.GeminiTranscriptParser") as parser_cls:
        parser_cls.return_value.parse_session_json.return_value = [
            _message(message_id="gemini-msg", input_tokens=200, output_tokens=50)
        ]
        await lifecycle._process_session_transcript(gemini_session.id, str(gemini_path))

    breakdown = TokenEventStore(temp_db).get_breakdown(project_id=project.id)

    assert breakdown["by_source"]["codex"]["input_tokens"] == 123
    assert breakdown["by_source"]["gemini"]["input_tokens"] == 200
    assert "gpt-5-codex" in breakdown["by_model"]
    assert "gemini-2.5-pro" in breakdown["by_model"]
    assert "unknown" not in breakdown["by_model"]
