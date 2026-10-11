"""Synthetic Claude messages must preserve the session's observed model."""

import json
from typing import Any

import pytest

from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("with_usage", [False, True])
async def test_final_synthetic_message_preserves_stored_model(
    temp_db: Any,
    sample_project: dict[str, Any],
    with_usage: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_local_machine_id(monkeypatch, "21000000-0000-4000-8000-000000000001")
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="synthetic-model-session",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    real_model = "claude-fable-5-1"
    sessions.update_model(session.id, real_model)
    synthetic: dict[str, Any] = {
        "role": "assistant",
        "model": "<synthetic>",
        "content": [{"type": "text", "text": "API error"}],
    }
    if with_usage:
        synthetic["usage"] = {"input_tokens": 1, "output_tokens": 1}
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "uuid": message_id,
                "timestamp": "2026-10-11T16:00:00Z",
                "message": message,
            }
        )
        for message_id, message in [
            (
                "real-message",
                {
                    "role": "assistant",
                    "model": real_model,
                    "content": [{"type": "text", "text": "Working"}],
                },
            ),
            ("synthetic-message", synthetic),
        ]
    ]
    messages = [
        record
        for record in ClaudeTranscriptParser().parse_lines(lines)
        if isinstance(record, ParsedMessage)
    ]
    assert len(messages) == 2
    processor = SessionMessageProcessor(temp_db, session_manager=sessions)

    await processor._persist_usage_events(session.id, messages)

    stored = sessions.get(session.id)
    assert stored is not None
    assert stored.model == real_model
