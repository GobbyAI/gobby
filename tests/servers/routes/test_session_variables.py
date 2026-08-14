"""HTTP tests for session variable get/set (plan 5.1)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import AgentApiTokenClaims
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


_OWN_SESSION = "11111111-1111-4111-8111-111111111111"
_OTHER_SESSION = "22222222-2222-4222-8222-222222222222"
_PROJECT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _client_with_agent_claims(
    session_id: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, AgentApiTokenClaims]:
    mock_sm = MagicMock()
    mock_sm.db = MagicMock()
    server = create_http_server(
        config=DaemonConfig(),
        database=mock_sm.db,
        session_manager=mock_sm,
    )
    claims = AgentApiTokenClaims(
        session_id=session_id,
        project_id=_PROJECT,
        iat=1,
        exp=4_102_444_800,
    )
    monkeypatch.setattr(
        server.auth_service,
        "verified_agent_claims",
        lambda _request: claims,
    )
    return TestClient(server.app), claims


def test_agent_token_cannot_set_other_session_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _claims = _client_with_agent_claims(_OWN_SESSION, monkeypatch)
    with (
        patch(
            "gobby.mcp_proxy.tools.workflows._variables.set_variable",
            return_value={"success": True},
        ) as mock_set,
        patch(
            "gobby.servers.routes.sessions.variables.resolve_session_reference",
            return_value=_OTHER_SESSION,
        ),
    ):
        resp = client.post(
            f"/api/sessions/{_OTHER_SESSION}/variables/set",
            json={"name": "foo", "value": "bar"},
        )
    assert resp.status_code == 403
    mock_set.assert_not_called()


def test_agent_token_cannot_get_other_session_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _claims = _client_with_agent_claims(_OWN_SESSION, monkeypatch)
    with (
        patch(
            "gobby.mcp_proxy.tools.workflows._variables.get_variable",
            return_value={"success": True, "value": None},
        ) as mock_get,
        patch(
            "gobby.servers.routes.sessions.variables.resolve_session_reference",
            return_value=_OTHER_SESSION,
        ),
    ):
        resp = client.post(
            f"/api/sessions/{_OTHER_SESSION}/variables/get",
            json={"name": "foo"},
        )
    assert resp.status_code == 403
    mock_get.assert_not_called()


def test_agent_token_can_set_own_session_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _claims = _client_with_agent_claims(_OWN_SESSION, monkeypatch)
    with patch(
        "gobby.mcp_proxy.tools.workflows._variables.set_variable",
        return_value={"success": True},
    ) as mock_set:
        resp = client.post(
            f"/api/sessions/{_OWN_SESSION}/variables/set",
            json={"name": "foo", "value": "bar"},
        )
    assert resp.status_code == 200
    mock_set.assert_called_once()
    assert mock_set.call_args.kwargs["session_id"] == _OWN_SESSION


def test_agent_token_hash_ref_resolves_only_to_claimed_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _claims = _client_with_agent_claims(_OWN_SESSION, monkeypatch)
    with (
        patch(
            "gobby.mcp_proxy.tools.workflows._variables.set_variable",
            return_value={"success": True},
        ) as mock_set,
        patch(
            "gobby.servers.routes.sessions.variables.resolve_session_reference",
            return_value=_OWN_SESSION,
        ),
    ):
        resp = client.post(
            "/api/sessions/%231/variables/set",
            json={"name": "foo", "value": "bar"},
        )
    assert resp.status_code == 200
    assert mock_set.call_args.kwargs["session_id"] == _OWN_SESSION
