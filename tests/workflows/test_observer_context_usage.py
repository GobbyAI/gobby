"""Grok session updates must carry a window and a usage ratio the handoff gate can read."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest

from gobby.llm.model_registry import ModelInfo
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.context_usage_snapshot import ContextUsageSnapshot
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.model_metadata import ModelMetadataStore
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class _TokenTotals:
    def get_session_totals(self, _session_id: str) -> dict[str, int]:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }

    def record(self, _event: object) -> bool:
        return False


class _RecordingSessions:
    def __init__(self, session: SimpleNamespace) -> None:
        self._session = session
        self.context_snapshot: ContextUsageSnapshot | None = None

    def get(self, _session_id: str) -> SimpleNamespace:
        return self._session

    def update_context_usage(self, _session_id: str, snapshot: ContextUsageSnapshot) -> bool:
        self.context_snapshot = snapshot
        return True

    def update_usage(self, **_kwargs: object) -> bool:
        return True

    def update_model(self, _session_id: str, model: str) -> bool:
        self._session.model = model
        return True


@pytest.mark.asyncio
async def test_grok_session_gets_context_usage_ratio(postgres_db: HubDatabase) -> None:
    """A grok-4.7 update with used tokens resolves the registry window and a ratio."""
    ModelMetadataStore(postgres_db).populate(
        [
            ModelInfo(
                id="grok-4.7",
                name="Grok 4.7",
                context_length=500_000,
                max_completion_tokens=None,
            )
        ]
    )
    sessions = _RecordingSessions(
        SimpleNamespace(
            project_id="proj-grok",
            source="grok",
            context_window=None,
            model=None,
            context_usage_confidence=None,
            context_used_tokens=None,
        )
    )
    processor = SessionMessageProcessor(
        postgres_db,
        session_manager=cast(SessionManager, sessions),
    )
    message = ParsedMessage(
        index=0,
        role="user",
        content="hello from a current grok-4.7 transcript",
        content_type="text",
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=datetime.now(UTC),
        raw_json={
            "method": "session/update",
            "params": {
                "_meta": {"eventId": "evt-current", "totalTokens": 10_000},
                "update": {"_meta": {"modelId": "grok-4.7"}},
            },
        },
        usage=None,
        model="grok-4.7",
        message_id="evt-current",
        context_used_tokens=10_000,
    )

    with (
        patch.object(processor, "_new_token_event_store", return_value=_TokenTotals()),
        patch("gobby.app_context.get_app_context", return_value=None),
    ):
        await processor._persist_usage_events("grok-session", [message])

    snapshot = sessions.context_snapshot
    assert snapshot is not None
    assert snapshot.context_window == 500_000
    assert snapshot.context_usage_ratio == 10_000 / 500_000
    assert snapshot.model == "grok-4.7"
