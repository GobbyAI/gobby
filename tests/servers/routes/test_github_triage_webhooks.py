from __future__ import annotations

import hmac
import json
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from gobby.github_triage.service import WebhookAcceptance
from gobby.servers.routes.github_triage import create_github_triage_router
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
            sync_enabled=True,
            triage_enabled=True,
            webhook_enabled=True,
            repositories=("owner/repo",),
            webhook_secret_ref=secret,
        )
    )
    raw_body = json.dumps({"zen": "Approachable is better than simple."}).encode()
    server = create_http_server(
        session_manager=session_manager,
        database=temp_db,
        authenticated_requests=False,
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


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "delivery-route-bad",
            "X-Hub-Signature-256": "sha256=invalid",
        },
    ],
)
def test_github_triage_webhook_rejects_missing_or_bad_signature(
    temp_db,
    session_manager,
    sample_project,
    headers: dict[str, str],
) -> None:
    secret = "route-secret"
    GitHubTriageStore(temp_db).upsert_config(
        GitHubTriageConfig(
            project_id=sample_project["id"],
            sync_enabled=True,
            triage_enabled=True,
            webhook_enabled=True,
            repositories=("owner/repo",),
            webhook_secret_ref=secret,
        )
    )
    server = create_http_server(
        session_manager=session_manager,
        database=temp_db,
        authenticated_requests=False,
    )

    response = TestClient(server.app).post(
        f"/api/github/webhooks/triage/{sample_project['id']}",
        content=b"{}",
        headers=headers,
    )

    assert response.status_code == 401


def test_github_triage_webhook_authentication_failures_are_indistinguishable(
    temp_db,
    session_manager,
    sample_project,
    caplog,
) -> None:
    secret = "route-secret-must-not-leak"
    store = GitHubTriageStore(temp_db)
    config = GitHubTriageConfig(
        project_id=sample_project["id"],
        sync_enabled=True,
        triage_enabled=True,
        webhook_enabled=True,
        repositories=("owner/repo",),
        webhook_secret_ref=secret,
    )
    store.upsert_config(config)
    server = create_http_server(
        session_manager=session_manager,
        database=temp_db,
        authenticated_requests=False,
    )
    client = TestClient(server.app)
    raw_body = b"{}"
    endpoint = f"/api/github/webhooks/triage/{sample_project['id']}"

    missing_signature = client.post(endpoint, content=raw_body)
    invalid_signature = client.post(
        endpoint,
        content=raw_body,
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "delivery-invalid",
            "X-Hub-Signature-256": "sha256=invalid",
        },
    )
    unknown_project = client.post(
        "/api/github/webhooks/triage/unknown-project",
        content=raw_body,
        headers=_signed_headers(raw_body, "attacker-controlled-secret"),
    )
    valid_signature = client.post(
        endpoint,
        content=raw_body,
        headers=_signed_headers(raw_body, secret),
    )
    store.upsert_config(
        GitHubTriageConfig(
            project_id=config.project_id,
            sync_enabled=False,
            triage_enabled=False,
            webhook_enabled=True,
            repositories=config.repositories,
            webhook_secret_ref=secret,
        )
    )
    disabled_triage = client.post(
        endpoint,
        content=raw_body,
        headers=_signed_headers(raw_body, secret),
    )

    failures = [missing_signature, invalid_signature, unknown_project, disabled_triage]
    assert {response.status_code for response in failures} == {401}
    assert {response.content for response in failures} == {
        b'{"detail":"GitHub webhook authentication failed"}'
    }
    assert valid_signature.status_code == 202
    assert all(secret not in response.text for response in [*failures, valid_signature])
    assert secret not in caplog.text


class _WebhookRequest:
    headers = {"x-github-event": "issues"}

    async def body(self) -> bytes:
        return b"{}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accepted",
    [
        WebhookAcceptance("delivery-processed", "ping", None, "processed"),
        WebhookAcceptance("delivery-ignored", "issues", "edited", "ignored"),
        WebhookAcceptance("delivery-duplicate", "issues", "opened", "pending", duplicate=True),
    ],
)
async def test_github_triage_webhook_early_returns_schedule_no_dispatch(
    accepted: WebhookAcceptance,
) -> None:
    """Processed, ignored, and duplicate webhook deliveries do not schedule dispatch."""
    server = SimpleNamespace(run_db=AsyncMock(return_value=accepted))
    background_tasks = BackgroundTasks()
    endpoint = create_github_triage_router(server).routes[0].endpoint

    with patch(
        "gobby.servers.routes.github_triage._service",
        return_value=SimpleNamespace(accept_webhook_delivery=object()),
    ):
        response = await endpoint("project-1", _WebhookRequest(), background_tasks)

    assert response["status"] == accepted.status
    assert response["duplicate"] is accepted.duplicate
    assert background_tasks.tasks == []
