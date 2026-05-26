"""Regression tests for hook ingress platform session metadata."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def _post_claude_hook(temp_db: HubDatabase, payload: dict, headers: dict | None = None) -> dict:
    session_manager = SessionManager(temp_db)
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_manager,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as adapter_cls,
    ):
        adapter = MagicMock()
        adapter.handle_native.return_value = {"continue": True}
        adapter_cls.return_value = adapter

        response = client.post("/api/hooks/execute", json=payload, headers=headers or {})

    assert response.status_code == 200
    return adapter.handle_native.call_args.args[0]


def test_real_session_header_is_passed_to_adapter_payload(temp_db: HubDatabase) -> None:
    adapter_payload = _post_claude_hook(
        temp_db,
        {
            "hook_type": "session-start",
            "source": "claude",
            "input_data": {"session_id": "claude-external"},
        },
        headers={"X-Gobby-Session-Id": "platform-session"},
    )

    assert adapter_payload["_platform_session_id"] == "platform-session"
    assert adapter_payload["input_data"]["session_id"] == "claude-external"


def test_envelope_headers_cannot_override_real_session_header(temp_db: HubDatabase) -> None:
    adapter_payload = _post_claude_hook(
        temp_db,
        {
            "schema_version": 1,
            "headers": {"X-Gobby-Session-Id": "embedded-session"},
            "hook_type": "session-start",
            "source": "claude",
            "input_data": {"session_id": "claude-external"},
        },
        headers={"X-Gobby-Session-Id": "real-session"},
    )

    assert adapter_payload["_platform_session_id"] == "real-session"


def test_embedded_envelope_headers_are_ignored_without_real_header(
    temp_db: HubDatabase,
) -> None:
    adapter_payload = _post_claude_hook(
        temp_db,
        {
            "schema_version": 1,
            "headers": {"X-Gobby-Session-Id": "embedded-session"},
            "hook_type": "session-start",
            "source": "claude",
            "input_data": {"session_id": "claude-external"},
        },
    )

    assert "_platform_session_id" not in adapter_payload
