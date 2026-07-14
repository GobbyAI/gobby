from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gobby.storage.github_triage import GitHubTriageStore
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def test_project_github_triage_config_round_trip(
    temp_db,
    session_manager,
    sample_project,
) -> None:
    server = create_http_server(
        session_manager=session_manager,
        database=temp_db,
    )
    client = TestClient(server.app)

    response = client.put(
        f"/api/projects/{sample_project['id']}/github-triage",
        json={
            "enabled": True,
            "webhook_enabled": True,
            "repositories": ["owner/repo"],
            "reconcile_interval_seconds": 1800,
            "webhook_secret_ref": "$secret:github_triage_webhook",
        },
    )
    assert response.status_code == 200

    fetched = client.get(f"/api/projects/{sample_project['id']}/github-triage")
    assert fetched.status_code == 200
    data = fetched.json()
    assert data["enabled"] is True
    assert data["webhook_enabled"] is True
    assert data["repositories"] == ["owner/repo"]
    assert data["reconcile_interval_seconds"] == 1800
    assert "webhook_secret_ref" not in data
    assert data["webhook_secret_configured"] is True
    assert "$secret:github_triage_webhook" not in response.text
    assert "$secret:github_triage_webhook" not in fetched.text
    assert (
        GitHubTriageStore(temp_db).get_config(sample_project["id"]).webhook_secret_ref
        == "$secret:github_triage_webhook"
    )

    round_trip = client.put(
        f"/api/projects/{sample_project['id']}/github-triage",
        json={"enabled": False},
    )
    assert round_trip.status_code == 200
    assert "$secret:github_triage_webhook" not in round_trip.text
    round_trip_data = round_trip.json()
    assert round_trip_data["webhook_enabled"] is True
    assert round_trip_data["repositories"] == ["owner/repo"]
    assert round_trip_data["reconcile_interval_seconds"] == 1800
    assert (
        GitHubTriageStore(temp_db).get_config(sample_project["id"]).webhook_secret_ref
        == "$secret:github_triage_webhook"
    )


@pytest.mark.parametrize(
    "field",
    [
        "enabled",
        "webhook_enabled",
        "repositories",
        "reconcile_interval_seconds",
    ],
)
def test_project_github_triage_config_rejects_explicit_null(
    temp_db,
    session_manager,
    sample_project,
    field: str,
) -> None:
    server = create_http_server(session_manager=session_manager, database=temp_db)
    client = TestClient(server.app)

    response = client.put(
        f"/api/projects/{sample_project['id']}/github-triage",
        json={field: None},
    )

    assert response.status_code == 422


def test_project_github_triage_config_allows_clearing_webhook_secret_ref(
    temp_db,
    session_manager,
    sample_project,
) -> None:
    server = create_http_server(session_manager=session_manager, database=temp_db)
    client = TestClient(server.app)
    route = f"/api/projects/{sample_project['id']}/github-triage"

    configured = client.put(route, json={"webhook_secret_ref": "$secret:webhook"})
    cleared = client.put(route, json={"webhook_secret_ref": None})

    assert configured.status_code == 200
    assert cleared.status_code == 200
    assert GitHubTriageStore(temp_db).get_config(sample_project["id"]).webhook_secret_ref is None


def test_project_github_triage_config_never_echoes_inline_secret(
    temp_db,
    session_manager,
    sample_project,
) -> None:
    server = create_http_server(session_manager=session_manager, database=temp_db)
    client = TestClient(server.app)
    secret = "inline-webhook-secret"

    updated = client.put(
        f"/api/projects/{sample_project['id']}/github-triage",
        json={"webhook_secret_ref": secret},
    )
    fetched = client.get(f"/api/projects/{sample_project['id']}/github-triage")

    assert updated.status_code == 200
    assert fetched.status_code == 200
    assert secret not in updated.text
    assert secret not in fetched.text
    assert "webhook_secret_ref" not in updated.json()
    assert "webhook_secret_ref" not in fetched.json()
    assert GitHubTriageStore(temp_db).get_config(sample_project["id"]).webhook_secret_ref == secret


def test_project_github_triage_config_rejects_non_positive_interval(
    temp_db,
    session_manager,
    sample_project,
) -> None:
    server = create_http_server(
        session_manager=session_manager,
        database=temp_db,
    )
    client = TestClient(server.app)

    response = client.put(
        f"/api/projects/{sample_project['id']}/github-triage",
        json={"reconcile_interval_seconds": 0},
    )

    assert response.status_code == 400
