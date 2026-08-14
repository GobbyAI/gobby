"""HTTP tests for session variable get/set (plan 5.1)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.hub.protocol import HubDatabase
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def client_without_session_manager(temp_db: HubDatabase) -> TestClient:
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=None,
    )
    return TestClient(server.app)


def _client_with_session_manager(database: object | None = None) -> TestClient:
    mock_sm = MagicMock()
    mock_sm.db = database if database is not None else MagicMock()
    server = create_http_server(
        config=DaemonConfig(),
        database=mock_sm.db,
        session_manager=mock_sm,
    )
    return TestClient(server.app)


def test_set_variable_no_session_manager(client_without_session_manager: TestClient) -> None:
    resp = client_without_session_manager.post(
        "/api/sessions/%231/variables/set",
        json={"name": "foo", "value": "bar"},
    )
    assert resp.status_code == 503


def test_get_variable_no_session_manager(client_without_session_manager: TestClient) -> None:
    resp = client_without_session_manager.post(
        "/api/sessions/%231/variables/get",
        json={"name": "foo"},
    )
    assert resp.status_code == 503


def test_old_workflow_variable_routes_are_gone(
    client_without_session_manager: TestClient,
) -> None:
    assert (
        client_without_session_manager.post(
            "/api/workflows/variables/set",
            json={"name": "foo", "value": "bar", "session_id": "#1"},
        ).status_code
        == 404
    )
    assert (
        client_without_session_manager.post(
            "/api/workflows/variables/get",
            json={"name": "foo", "session_id": "#1"},
        ).status_code
        == 404
    )


def test_set_variable_with_session_manager(temp_db: HubDatabase) -> None:
    client = _client_with_session_manager(temp_db)
    with patch(
        "gobby.mcp_proxy.tools.workflows._variables.set_variable",
        return_value={"success": True},
    ):
        resp = client.post(
            "/api/sessions/%231/variables/set",
            json={"name": "foo", "value": "bar"},
        )
    assert resp.status_code == 200


def test_set_variable_accepts_json_values() -> None:
    client = _client_with_session_manager()
    with patch(
        "gobby.mcp_proxy.tools.workflows._variables.set_variable",
        return_value={"success": True},
    ) as mock_set:
        resp = client.post(
            "/api/sessions/%231/variables/set",
            json={"name": "loaded_skills", "value": ["tasks"]},
        )
    assert resp.status_code == 200
    mock_set.assert_called_once()
    assert mock_set.call_args.kwargs["value"] == ["tasks"]
    assert "workflow" not in mock_set.call_args.kwargs


def test_set_variable_accepts_step_scope() -> None:
    client = _client_with_session_manager()
    with patch(
        "gobby.mcp_proxy.tools.workflows._variables.set_variable",
        return_value={"success": True},
    ) as mock_set:
        resp = client.post(
            "/api/sessions/%231/variables/set",
            json={
                "name": "implementation_complete",
                "value": True,
                "scope": "step",
            },
        )
    assert resp.status_code == 200
    mock_set.assert_called_once()
    assert mock_set.call_args.kwargs["scope"] == "step"
    assert mock_set.call_args.kwargs["session_id"] == "#1"


def test_get_variable_accepts_step_scope() -> None:
    client = _client_with_session_manager()
    with patch(
        "gobby.mcp_proxy.tools.workflows._variables.get_variable",
        return_value={"success": True, "value": True},
    ) as mock_get:
        resp = client.post(
            "/api/sessions/%231/variables/get",
            json={"name": "implementation_complete", "scope": "step"},
        )
    assert resp.status_code == 200
    mock_get.assert_called_once()
    assert mock_get.call_args.kwargs["scope"] == "step"
    assert mock_get.call_args.kwargs["session_id"] == "#1"


def test_get_variable_with_session_manager(temp_db: HubDatabase) -> None:
    client = _client_with_session_manager(temp_db)
    with patch(
        "gobby.mcp_proxy.tools.workflows._variables.get_variable",
        return_value={"success": True, "value": None},
    ):
        resp = client.post(
            "/api/sessions/%231/variables/get",
            json={"name": "foo"},
        )
    assert resp.status_code == 200
