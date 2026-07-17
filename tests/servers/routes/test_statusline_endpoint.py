"""Tests for the POST /api/sessions/statusline endpoint."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from gobby.servers.routes.sessions import create_sessions_router, statusline_activity

pytestmark = pytest.mark.unit

NOW_ISO = "2026-03-17T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _reset_statusline_trackers():
    """Keep module-level trackers isolated per test."""
    statusline_activity.reset_for_tests()
    yield
    statusline_activity.reset_for_tests()


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
    server.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
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

        with patch("gobby.servers.routes.sessions.core.inc_counter") as mock_counter:
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
        mock_counter.assert_called_once_with(
            "statusline_posts_succeeded_total", attributes={"source": "claude"}
        )

    def test_preserves_one_million_session_model_in_storage_and_broadcast(
        self, client, mock_server
    ) -> None:
        session = _make_session(model="claude-opus-4-8[1m]")
        mock_server.session_manager.find_active_by_external_id.return_value = session

        response = client.post(
            "/api/sessions/statusline",
            json={
                "session_id": "ext-123",
                "model_id": "claude-opus-4-8",
                "input_tokens": 125_071,
                "context_window_size": 200_000,
            },
        )

        assert response.status_code == 200
        update_call = mock_server.session_manager.update_usage.call_args
        assert update_call.kwargs["model"] == "claude-opus-4-8[1m]"
        assert update_call.kwargs["context_window"] == 1_000_000
        broadcast = mock_server.services.websocket_server.broadcast_session_usage_updated
        payload = broadcast.call_args.args[0]
        assert payload["model"] == "claude-opus-4-8[1m]"
        assert payload["context_window"] == 1_000_000

    def test_prunes_statusline_trackers_once_without_changing_activity(
        self, client, mock_server
    ) -> None:
        session = _make_session()
        mock_server.session_manager.find_active_by_external_id.return_value = session
        mock_server.session_manager.update_usage.return_value = True
        now = datetime(2026, 3, 17, 12, 0, 0, tzinfo=UTC)
        stale_at = now - timedelta(seconds=statusline_activity.STATUSLINE_LAST_SEEN_TTL_SECONDS + 1)
        activity_at = now - timedelta(seconds=30)
        statusline_activity.record_session_activity(session.id, activity_at)
        statusline_activity._STATUSLINE_LAST_SEEN["stale-session"] = stale_at
        statusline_activity._LAST_PRUNE_AT = now - timedelta(
            seconds=statusline_activity.STATUSLINE_PRUNE_INTERVAL_SECONDS
        )
        mock_prune = MagicMock(wraps=statusline_activity.prune_trackers)

        with (
            patch("gobby.servers.routes.sessions.core.datetime", autospec=True) as mock_datetime,
            patch(
                "gobby.servers.routes.sessions.core.prune_trackers",
                mock_prune,
                create=True,
            ),
            patch.object(statusline_activity, "prune_trackers", mock_prune),
        ):
            mock_datetime.now.return_value = now
            response = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})

        assert response.status_code == 200
        mock_prune.assert_called_once_with(now)
        assert "stale-session" not in statusline_activity._STATUSLINE_LAST_SEEN
        assert statusline_activity.last_session_activity(session.id) == activity_at

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
                content=b'{"session_id":"ext-123"}',
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

    @pytest.mark.parametrize(
        "field",
        [
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "context_window_size",
        ],
    )
    def test_rejects_invalid_usage_values(self, client, mock_server, field: str) -> None:
        response = client.post(
            "/api/sessions/statusline",
            json={"session_id": "ext-123", field: "invalid"},
        )

        assert response.status_code == 422
        mock_server.session_manager.find_active_by_external_id.assert_not_called()
        mock_server.session_manager.update_usage.assert_not_called()

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

    def test_warns_for_anomalous_gap_with_concurrent_session_activity(
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
            patch("gobby.servers.routes.sessions.core.inc_counter") as mock_counter,
            caplog.at_level(logging.WARNING, logger="gobby.servers.routes.sessions.core"),
        ):
            mock_datetime.now.side_effect = [start, start + timedelta(seconds=605)]

            response_one = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})
            # A hook event landed after the first statusline POST: session is alive
            # while the statusline feed is silent — this is the actionable case.
            statusline_activity.record_session_activity(session.id, start + timedelta(seconds=60))
            response_two = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})

        assert response_one.status_code == 200
        assert response_two.status_code == 200
        assert "statusline_usage_gap" in caplog.text
        assert "gap_ms=605000" in caplog.text
        assert "threshold_ms=600000" in caplog.text
        mock_counter.assert_any_call(
            "statusline_usage_gap_warnings_total", attributes={"source": "claude"}
        )
        mock_counter.assert_any_call(
            "statusline_posts_succeeded_total", attributes={"source": "claude"}
        )

    def test_suppresses_warning_for_short_active_gap(
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
            patch("gobby.servers.routes.sessions.core.inc_counter") as mock_counter,
            caplog.at_level(logging.WARNING, logger="gobby.servers.routes.sessions.core"),
        ):
            mock_datetime.now.side_effect = [start, start + timedelta(seconds=125)]

            response_one = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})
            statusline_activity.record_session_activity(session.id, start + timedelta(seconds=60))
            response_two = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})

        assert response_one.status_code == 200
        assert response_two.status_code == 200
        assert "statusline_usage_gap" not in caplog.text
        assert (
            "statusline_usage_gap_warnings_total",
            {"attributes": {"source": "claude"}},
        ) not in [(call.args[0], call.kwargs) for call in mock_counter.call_args_list if call.args]

    def test_throttles_repeated_anomalous_gap_warnings(
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
            patch("gobby.servers.routes.sessions.core.inc_counter") as mock_counter,
            caplog.at_level(logging.WARNING, logger="gobby.servers.routes.sessions.core"),
        ):
            mock_datetime.now.side_effect = [
                start,
                start + timedelta(seconds=605),
                start + timedelta(seconds=1210),
            ]

            response_one = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})
            statusline_activity.record_session_activity(session.id, start + timedelta(seconds=60))
            response_two = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})
            statusline_activity.record_session_activity(session.id, start + timedelta(seconds=610))
            response_three = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})

        assert response_one.status_code == 200
        assert response_two.status_code == 200
        assert response_three.status_code == 200
        assert caplog.text.count("statusline_usage_gap session_id=") == 1
        warning_counter_calls = [
            call
            for call in mock_counter.call_args_list
            if call.args and call.args[0] == "statusline_usage_gap_warnings_total"
        ]
        assert len(warning_counter_calls) == 1

    def test_suppresses_gap_when_session_is_otherwise_quiet(
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
            mock_datetime.now.side_effect = [start, start + timedelta(seconds=200)]

            response_one = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})
            response_two = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})

        assert response_one.status_code == 200
        assert response_two.status_code == 200
        assert "statusline_usage_gap" not in caplog.text

    def test_suppresses_gap_when_activity_predates_previous_statusline(
        self, client, mock_server, caplog, enable_log_propagation
    ) -> None:
        session = _make_session()
        mock_server.session_manager.find_active_by_external_id.return_value = session
        mock_server.session_manager.update_usage.return_value = True
        start = datetime(2026, 3, 17, 12, 0, 0, tzinfo=UTC)

        # Record activity before the first statusline POST so it's older than `previous`.
        statusline_activity.record_session_activity(session.id, start - timedelta(seconds=30))

        with (
            patch(
                "gobby.servers.routes.sessions.core.datetime",
                autospec=True,
            ) as mock_datetime,
            caplog.at_level(logging.WARNING, logger="gobby.servers.routes.sessions.core"),
        ):
            mock_datetime.now.side_effect = [start, start + timedelta(seconds=130)]

            response_one = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})
            response_two = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})

        assert response_one.status_code == 200
        assert response_two.status_code == 200
        assert "statusline_usage_gap" not in caplog.text

    def test_no_warning_when_gap_under_threshold_even_with_activity(
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
            mock_datetime.now.side_effect = [start, start + timedelta(seconds=60)]

            response_one = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})
            statusline_activity.record_session_activity(session.id, start + timedelta(seconds=30))
            response_two = client.post("/api/sessions/statusline", json={"session_id": "ext-123"})

        assert response_one.status_code == 200
        assert response_two.status_code == 200
        assert "statusline_usage_gap" not in caplog.text
