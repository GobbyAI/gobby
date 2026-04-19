"""Tests for the POST /api/sessions/statusline endpoint."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from gobby.servers.routes.sessions import create_sessions_router

pytestmark = pytest.mark.unit

NOW_ISO = "2026-03-17T12:00:00+00:00"


def _make_session(**overrides) -> MagicMock:
    defaults = {
        "id": "sess-abc123",
        "external_id": "ext-123",
        "machine_id": "machine-1",
        "source": "claude",
        "project_id": "proj-123",
        "title": "Test Session",
        "status": "active",
        "transcript_path": "/tmp/test.jsonl",
        "summary_path": None,
        "summary_markdown": None,
        "git_branch": "main",
        "parent_session_id": None,
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
        "seq_num": 42,
    }
    defaults.update(overrides)
    session = MagicMock()
    for key, val in defaults.items():
        setattr(session, key, val)
    session.to_dict.return_value = defaults
    return session


@pytest.fixture
def mock_server():
    server = MagicMock()
    server.session_manager = MagicMock()
    server.session_manager.db = MagicMock()
    server.message_manager = AsyncMock()
    server.llm_service = MagicMock()
    server.resolve_project_id = MagicMock(return_value="proj-123")
    return server


@pytest.fixture
def mock_hook_manager():
    hook_manager = MagicMock()
    hook_manager._stop_registry = MagicMock()
    return hook_manager


@pytest.fixture
def client(mock_server, mock_hook_manager):
    app = FastAPI()
    router = create_sessions_router(mock_server)
    app.include_router(router)
    app.state.hook_manager = mock_hook_manager
    return TestClient(app)


class TestStatuslineEndpoint:
    """Tests for POST /statusline endpoint."""

    def test_updates_usage_for_known_session(self, client, mock_server) -> None:
        session = _make_session()
        mock_server.session_manager.find_active_by_external_id.return_value = session
        mock_server.session_manager.update_usage.return_value = True

        response = client.post(
            "/api/sessions/statusline",
            json={
                "session_id": "ext-123",
                "model_id": "claude-opus-4-6",
                "input_tokens": 12345,
                "output_tokens": 6789,
                "cache_creation_tokens": 1000,
                "cache_read_tokens": 5000,
                "context_window_size": 200000,
            },
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

        mock_server.session_manager.find_active_by_external_id.assert_called_once_with(
            "ext-123", source="claude"
        )
        mock_server.session_manager.update_usage.assert_called_once_with(
            session_id="sess-abc123",
            input_tokens=12345,
            output_tokens=6789,
            cache_creation_tokens=1000,
            cache_read_tokens=5000,
            context_window=200000,
            model="claude-opus-4-6",
        )

    def test_returns_warning_for_unknown_session(self, client, mock_server) -> None:
        mock_server.session_manager.find_active_by_external_id.return_value = None

        response = client.post(
            "/api/sessions/statusline",
            json={
                "session_id": "unknown-session",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["warning"] == "session_not_found"

    def test_rejects_missing_session_id(self, client) -> None:
        response = client.post(
            "/api/sessions/statusline",
            json={"input_tokens": 100},
        )
        assert response.status_code == 400

    def test_rejects_invalid_json(self, client) -> None:
        response = client.post(
            "/api/sessions/statusline",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400

    def test_ignores_client_disconnect_while_reading_json(self, client, mock_server) -> None:
        with patch(
            "starlette.requests.Request.json",
            new=AsyncMock(side_effect=ClientDisconnect()),
        ):
            response = client.post(
                "/api/sessions/statusline",
                content=b"{\"session_id\":\"ext-123\"}",
                headers={"Content-Type": "application/json"},
            )

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "warning": "client_disconnected"}
        mock_server.session_manager.find_active_by_external_id.assert_not_called()
        mock_server.session_manager.update_usage.assert_not_called()

    def test_defaults_missing_fields(self, client, mock_server) -> None:
        session = _make_session()
        mock_server.session_manager.find_active_by_external_id.return_value = session
        mock_server.session_manager.update_usage.return_value = True

        response = client.post(
            "/api/sessions/statusline",
            json={
                "session_id": "ext-123",
            },
        )

        assert response.status_code == 200
        mock_server.session_manager.update_usage.assert_called_once_with(
            session_id="sess-abc123",
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            context_window=None,
            model=None,
        )

    def test_does_not_log_usage_gap_for_routine_updates(
        self, client, mock_server, caplog, enable_log_propagation
    ) -> None:
        session = _make_session()
        mock_server.session_manager.find_active_by_external_id.return_value = session
        mock_server.session_manager.update_usage.return_value = True
        start = datetime(2026, 3, 17, 12, 0, 0, tzinfo=UTC)

        with (
            patch(
                "gobby.servers.routes.sessions.core.datetime",
                autospec=True,
            ) as mock_datetime,
            caplog.at_level(logging.INFO, logger="gobby.servers.routes.sessions.core"),
        ):
            mock_datetime.now.side_effect = [start, start + timedelta(seconds=5)]

            response_one = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})
            response_two = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})

        assert response_one.status_code == 200
        assert response_two.status_code == 200
        assert "statusline_usage_gap" not in caplog.text

    def test_warns_for_large_statusline_update_gap(
        self, client, mock_server, caplog, enable_log_propagation
    ) -> None:
        session = _make_session()
        mock_server.session_manager.find_active_by_external_id.return_value = session
        mock_server.session_manager.update_usage.return_value = True
        start = datetime(2026, 3, 17, 12, 0, 0, tzinfo=UTC)

        with (
            patch(
                "gobby.servers.routes.sessions.core.datetime",
                autospec=True,
            ) as mock_datetime,
            caplog.at_level(logging.WARNING, logger="gobby.servers.routes.sessions.core"),
        ):
            mock_datetime.now.side_effect = [start, start + timedelta(seconds=31)]

            response_one = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})
            response_two = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})

        assert response_one.status_code == 200
        assert response_two.status_code == 200
        assert "statusline_usage_gap" in caplog.text
        assert "gap_ms=31000" in caplog.text
        assert "threshold_ms=30000" in caplog.text
