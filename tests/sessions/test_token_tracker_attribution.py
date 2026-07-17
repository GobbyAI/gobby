from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage
from gobby.storage.hub.protocol import HubDatabase
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
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Codex/Qwen token events should aggregate under their source and model names."""
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

    qwen_path = tmp_path / "qwen.json"
    qwen_path.write_text('{"sessionId":"qwen-ext","messages":[]}')
    qwen_session = session_manager.register(
        external_id="qwen-ext",
        machine_id="machine-1",
        source="qwen",
        project_id=project.id,
        transcript_path=str(qwen_path),
    )
    session_manager.update_usage(
        qwen_session.id,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        model="qwen3-coder",
    )

    # Keep this direct call paired with the Codex case above for stable
    # source/model attribution coverage.
    with patch("gobby.sessions.lifecycle.QwenTranscriptParser") as parser_cls:
        parser_cls.return_value.parse_session_json.return_value = [
            _message(message_id="qwen-msg", input_tokens=200, output_tokens=50)
        ]
        await lifecycle._process_session_transcript(qwen_session.id, str(qwen_path))

    breakdown = TokenEventStore(temp_db).get_breakdown(project_id=project.id)

    assert breakdown["by_source"]["codex"]["input_tokens"] == 123
    assert breakdown["by_source"]["qwen"]["input_tokens"] == 200
    assert "gpt-5-codex" in breakdown["by_model"]
    assert "qwen3-coder" in breakdown["by_model"]
    assert "unknown" not in breakdown["by_model"]
