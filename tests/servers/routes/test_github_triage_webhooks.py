from __future__ import annotations

import hmac
import json
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from gobby.storage.github_triage import GitHubTriageConfig, GitHubTriageStore
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def _signed_headers(raw_body: bytes, secret: str) -> dict[str, str]:
    signature = hmac.new(secret.encode(), raw_body, sha256).hexdigest()
    return {
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": "delivery-route-1",
        "X-Hub-Signature-256": f"sha256={signature}",
    }


def test_github_triage_webhook_persists_delivery_and_returns_202(
    temp_db,
    session_manager,
    sample_project,
) -> None:
    secret = "route-secret"
    GitHubTriageStore(temp_db).upsert_config(
        GitHubTriageConfig(
            project_id=sample_project["id"],
            enabled=True,
            webhook_enabled=True,
            repositories=("owner/repo",),
            webhook_secret_ref=secret,
        )
    )
    raw_body = json.dumps({"zen": "Approachable is better than simple."}).encode()
    server = create_http_server(
        session_manager=session_manager,
        database=temp_db,
    )

    response = TestClient(server.app).post(
        f"/api/github/webhooks/triage/{sample_project['id']}",
        content=raw_body,
        headers=_signed_headers(raw_body, secret),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "processed"
    delivery = GitHubTriageStore(temp_db).get_delivery(sample_project["id"], "delivery-route-1")
    assert delivery is not None
    assert delivery.event == "ping"
    assert delivery.status == "processed"
