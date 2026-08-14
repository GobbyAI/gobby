"""Grant-presenting runtime configuration transport."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.config.runtime import ConfigRuntime
from gobby.runtime_grants.handshake import HandshakeService, encode_grant_header
from gobby.runtime_grants.schema import PostgresDirect
from gobby.runtime_grants.service import DeploymentGrantContext, GrantService
from gobby.servers.auth_service import AuthService
from gobby.utils.local_token import issue_agent_api_token, verify_agent_api_token
from tests.runtime_grants.support import (
    DEPLOYMENT_TOKEN,
    FENCING_EPOCH,
    GOLDEN_SECRET,
    StaticRuntime,
    config_snapshot,
    daemon_config,
)
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

OPERATOR_TOKEN = "runtime-config-operator"
LOCAL_MACHINE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SESSION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
AGENT_RUN_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


@pytest.fixture(autouse=True)
def _machine_id() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _postgres() -> PostgresDirect:
    return PostgresDirect(
        dsn="postgresql://gobby_ix_test:secret@127.0.0.1:60892/gobby_test",
        role_name="gobby_ix_test",
        credential_generation=1,
        valid_until=1_700_003_600,
    )


def _services(config: DaemonConfig) -> tuple[GrantService, HandshakeService, Any]:
    snapshot = config_snapshot(config, revision=41)
    grants = GrantService(
        runtime=StaticRuntime(snapshot),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
        clock=lambda: 1_700_000_000,
    )
    handshake = HandshakeService(
        grants=grants,
        local_machine_id=LOCAL_MACHINE_ID,
        operator_token=OPERATOR_TOKEN,
        issue_postgres=_postgres,
        admitted_projects=frozenset({PROJECT_ID}),
        clock=lambda: 1_700_000_000,
    )
    return grants, handshake, snapshot


def _client(
    tmp_path: Path, config: DaemonConfig
) -> tuple[TestClient, GrantService, HandshakeService]:
    grants, handshake, snapshot = _services(config)
    server = create_http_server(config=config, authenticated_requests=False)
    token_file = tmp_path / "local_cli_token"
    token_file.write_text(OPERATOR_TOKEN)
    server.auth_service = AuthService(lambda: server.services.database, token_file=token_file)
    setattr(
        server.auth_service,
        "is_request_authenticated",
        lambda request: bool(request.headers.get("Authorization")),
    )
    server.grant_service = grants
    server.handshake_service = handshake
    runtime = MagicMock(spec=ConfigRuntime)
    runtime.snapshot = snapshot
    runtime.capture.return_value.snapshot = snapshot
    server.services.config_runtime = runtime
    return TestClient(server.app), grants, handshake


def test_grant_presenting_config_transport(tmp_path: Path) -> None:
    config = daemon_config()
    client, _grants, handshake = _client(tmp_path, config)
    operator_grant = handshake.issue_for_operator(
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        session_id=SESSION_ID,
    )
    agent_token = issue_agent_api_token(
        OPERATOR_TOKEN,
        agent_run_id=AGENT_RUN_ID,
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        machine_id=LOCAL_MACHINE_ID,
        timeout_seconds=30,
    )
    agent_grant = handshake.issue_for_agent(
        verify_agent_api_token(agent_token, OPERATOR_TOKEN),
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
    )

    operator = client.get(
        "/api/runtime/config",
        headers={
            "Authorization": f"Bearer {OPERATOR_TOKEN}",
            "X-Gobby-Runtime-Grant": encode_grant_header(operator_grant),
        },
    )
    agent = client.get(
        "/api/runtime/config",
        headers={
            "Authorization": f"Bearer {agent_token}",
            "X-Gobby-Runtime-Grant": encode_grant_header(agent_grant),
            "X-Gobby-Session-Id": SESSION_ID,
            "X-Gobby-Caller-Project-Id": PROJECT_ID,
            "X-Gobby-Agent-Run-Id": AGENT_RUN_ID,
        },
    )
    assert operator.status_code == 200
    assert agent.status_code == 200
    assert operator.json()["settings"] == agent.json()["settings"]
    assert "ai.embeddings.api_key" not in operator.json()["settings"]
    assert operator.json()["settings"]["databases.falkordb.host"] == config.databases.falkordb.host
    assert client.get("/api/runtime/config").status_code == 401


def test_config_revision_in_response(tmp_path: Path) -> None:
    client, _grants, handshake = _client(tmp_path, daemon_config())
    grant = handshake.issue_for_operator(
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        session_id=SESSION_ID,
    )
    response = client.get(
        "/api/runtime/config",
        headers={
            "Authorization": f"Bearer {OPERATOR_TOKEN}",
            "X-Gobby-Runtime-Grant": encode_grant_header(grant),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["config_revision"] == 41
    assert body["config_revision"] == grant.config_revision
    assert "settings" in body
