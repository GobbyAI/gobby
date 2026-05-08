from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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
    assert data["webhook_secret_ref"] == "$secret:github_triage_webhook"


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
