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
