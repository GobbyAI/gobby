"""Focused tests for AGY hook dispatch through the unified hooks route."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def session_storage(temp_db: HubDatabase) -> SessionManager:
    return SessionManager(temp_db)


def test_execute_hook_dispatches_agy_adapter(session_storage: SessionManager) -> None:
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_storage,
    )
    mock_hook_manager = MagicMock()
    mock_hook_manager.shutdown_async = AsyncMock()
    server.app.state.hook_manager = mock_hook_manager

    with (
        TestClient(server.app) as client,
        patch("gobby.adapters.agy.AgyAdapter") as MockAdapter,
    ):
        mock_adapter = MagicMock()
        mock_adapter.handle_native.return_value = {"decision": "allow"}
        MockAdapter.return_value = mock_adapter

        response = client.post(
            "/api/hooks/execute",
            json={
                "schema_version": 1,
                "enqueued_at": "2026-06-24T12:00:00Z",
                "critical": False,
                "hook_type": "PreToolUse",
                "source": "agy",
                "input_data": {
                    "hook_event_name": "PreToolUse",
                    "session_id": "agy-123",
                    "cwd": "/tmp",
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == {"decision": "allow"}
    MockAdapter.assert_called_once()
    adapter_hook_manager = MockAdapter.call_args.kwargs["hook_manager"]
    assert adapter_hook_manager is server.app.state.hook_manager
    assert mock_adapter.handle_native.call_args.args[0] == {
        "hook_type": "PreToolUse",
        "source": "agy",
        "input_data": {
            "hook_event_name": "PreToolUse",
            "session_id": "agy-123",
            "cwd": "/tmp",
        },
    }
    assert mock_adapter.handle_native.call_args.args[1] is adapter_hook_manager


def test_execute_hook_unsupported_source_lists_agy(
    session_storage: SessionManager,
) -> None:
    server = create_http_server(
        port=60887,
        test_mode=True,
        session_manager=session_storage,
    )
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()

    with TestClient(server.app) as client:
        response = client.post(
            "/api/hooks/execute",
            json={
                "schema_version": 1,
                "enqueued_at": "2026-06-24T12:00:00Z",
                "critical": False,
                "hook_type": "PreToolUse",
                "source": "unsupported",
                "input_data": {},
            },
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "Unsupported source" in detail
    assert "agy" in detail
