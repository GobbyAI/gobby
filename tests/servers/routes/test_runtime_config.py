"""Grant-presenting runtime configuration transport."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.config.runtime import ConfigRuntime
from gobby.runtime_grants.handshake import HandshakeService, encode_grant_header
from gobby.runtime_grants.launch import materialize_managed_launch, merge_child_env
from gobby.runtime_grants.schema import PostgresDirect
from gobby.runtime_grants.service import DeploymentGrantContext, GrantService
from gobby.servers.auth_service import AuthService
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import (
    derive_managed_signing_key,
    issue_agent_api_token,
    verify_agent_api_token,
)
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


def _postgres(now: int = 1_700_000_000) -> PostgresDirect:
    return PostgresDirect(
        dsn="postgresql://gobby_ix_test:secret@127.0.0.1:60892/gobby_test",
        role_name="gobby_ix_test",
        credential_generation=1,
        valid_until=now + 3600,
    )


def _services(
    config: DaemonConfig, now: int = 1_700_000_000
) -> tuple[GrantService, HandshakeService, Any]:
    snapshot = config_snapshot(config, revision=41)
    grants = GrantService(
        runtime=StaticRuntime(snapshot),
        context=DeploymentGrantContext(
            token=DEPLOYMENT_TOKEN,
            fencing_epoch=FENCING_EPOCH,
            signing_secret=GOLDEN_SECRET,
        ),
        clock=lambda: now,
    )
    handshake = HandshakeService(
        grants=grants,
        local_machine_id=LOCAL_MACHINE_ID,
        issue_postgres=lambda _principal: _postgres(now),
        admitted_projects=frozenset({PROJECT_ID}),
        clock=lambda: now,
    )
    return grants, handshake, snapshot


def _client(
    tmp_path: Path, config: DaemonConfig, now: int = 1_700_000_000
) -> tuple[TestClient, GrantService, HandshakeService]:
    grants, handshake, snapshot = _services(config, now)
    database = MagicMock()
    database.fetchone.return_value = {"status": "running", "login_capable": True}
    server = create_http_server(config=config, database=database, authenticated_requests=False)
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text(json.dumps({"api_key": OPERATOR_TOKEN}))
    server.auth_service = AuthService(
        lambda: server.services.database,
        bootstrap_file=bootstrap,
        break_glass_file=tmp_path / "absent-break-glass",
    )
    server.auth_service.bind_runtime(
        grant_service=grants,
        lease_live=None,
        local_machine_id=LOCAL_MACHINE_ID,
        effect_fence=None,
        clock=lambda: now,
        front_door_secret=_FRONT_DOOR_SECRET,
    )
    server.grant_service = grants
    server.handshake_service = handshake
    runtime = MagicMock(spec=ConfigRuntime)
    runtime.snapshot = snapshot
    runtime.capture.return_value.snapshot = snapshot
    server.services.config_runtime = runtime
    return TestClient(server.app), grants, handshake


def test_orphan_maintenance_config_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.servers.auth_service.resolve_auth_schema", lambda _: "auth")
    config = daemon_config()
    client, _grants, handshake = _client(tmp_path, config)
    orphan_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    handshake.admitted_maintenance_targets = lambda project_id: project_id == orphan_id
    grant = handshake.issue_for_maintenance(
        machine_id=LOCAL_MACHINE_ID,
        project_id=orphan_id,
        execution_id=AGENT_RUN_ID,
    )
    launch = materialize_managed_launch(
        grant,
        dest_dir=tmp_path / "maintenance",
        signing_key=derive_managed_signing_key(OPERATOR_TOKEN),
        deadline_seconds=30,
    )
    token = launch.env["GOBBY_AGENT_API_TOKEN"]
    claims = verify_agent_api_token(token, derive_managed_signing_key(OPERATOR_TOKEN))
    assert claims is not None
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Runtime-Grant": encode_grant_header(grant),
        "X-Gobby-Session-Id": claims.session_id,
        "X-Gobby-Caller-Project-Id": claims.project_id,
        "X-Gobby-Managed-Execution-Id": claims.managed_execution_id or "",
    }
    response = client.get("/api/runtime/config", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["ai.embeddings.model"] == config.embeddings.model
    for key, value in (
        ("X-Gobby-Caller-Project-Id", PROJECT_ID),
        ("X-Gobby-Session-Id", SESSION_ID),
        ("X-Gobby-Managed-Execution-Id", SESSION_ID),
    ):
        rejected = client.get("/api/runtime/config", headers={**headers, key: value})
        assert rejected.status_code == 401


@pytest.mark.integration
def test_native_orphan_maintenance_config_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    postgres_db: HubDatabase,
    postgres_database_url: str,
) -> None:
    binary = shutil.which("gcode")
    if binary is None:
        pytest.skip("native config regression requires installed gcode")
    monkeypatch.setattr("gobby.servers.auth_service.resolve_auth_schema", lambda _: "auth")
    config = daemon_config(qdrant_url="http://127.0.0.1:1")
    now = int(time.time())
    client, _grants, handshake = _client(tmp_path, config, now)
    schema = postgres_db.fetchone("SELECT current_schema() AS schema")
    assert schema is not None
    assert ":60892/" in postgres_database_url
    scoped_dsn = postgres_database_url + f"?options=-csearch_path%3D{schema['schema']}"
    handshake.issue_postgres = lambda _principal: _postgres(now).model_copy(
        update={
            "dsn": scoped_dsn,
            "role_name": "gobby_test",
        }
    )
    orphan_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    handshake.admitted_maintenance_targets = lambda project_id: project_id == orphan_id
    grant = handshake.issue_for_maintenance(
        machine_id=LOCAL_MACHINE_ID, project_id=orphan_id, execution_id=AGENT_RUN_ID
    )
    launch = materialize_managed_launch(
        grant,
        dest_dir=tmp_path / "maintenance",
        signing_key=derive_managed_signing_key(OPERATOR_TOKEN),
        deadline_seconds=30,
    )
    config_statuses: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/api/runtime/config":
                response = client.get(self.path, headers=dict(self.headers.items()))
                config_statuses.append(response.status_code)
                status, body = response.status_code, response.content
            else:
                status, body = 200, b"{}"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    home = tmp_path / "native-home"
    home.mkdir()
    (home / "machine_id").write_text(LOCAL_MACHINE_ID)
    child_env = merge_child_env(launch.env)
    assert child_env is not None
    child_env["GOBBY_HOME"] = str(home)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        child_env["GOBBY_DAEMON_URL"] = f"http://127.0.0.1:{server.server_port}"
        try:
            result = subprocess.run(
                [binary, "vector", "clear", "--project-id", orphan_id, "--drop-collection"],
                cwd=tmp_path,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
    # The fake Qdrant endpoint deliberately stops projection cleanup after config resolves.
    assert config_statuses == [200], result.stderr
    assert "managed runtime config fetch failed" not in result.stderr
    assert "embedding config is required" not in result.stderr


def test_grant_presenting_config_transport(tmp_path: Path) -> None:
    config = daemon_config()
    client, _grants, handshake = _client(tmp_path, config)
    operator_grant = handshake.issue_for_operator(
        machine_id=LOCAL_MACHINE_ID,
        forwarded_machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        session_id=SESSION_ID,
    )
    agent_token = issue_agent_api_token(
        derive_managed_signing_key(OPERATOR_TOKEN),
        agent_run_id=AGENT_RUN_ID,
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        machine_id=LOCAL_MACHINE_ID,
        timeout_seconds=30,
    )
    agent_claims = verify_agent_api_token(agent_token, derive_managed_signing_key(OPERATOR_TOKEN))
    assert agent_claims is not None
    agent_grant = handshake.issue_for_agent(
        agent_claims,
        machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
    )

    operator = client.get(
        "/api/runtime/config",
        headers={
            **_OPERATOR_IDENTITY,
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
        forwarded_machine_id=LOCAL_MACHINE_ID,
        project_id=PROJECT_ID,
        session_id=SESSION_ID,
    )
    response = client.get(
        "/api/runtime/config",
        headers={
            **_OPERATOR_IDENTITY,
            "X-Gobby-Runtime-Grant": encode_grant_header(grant),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["config_revision"] == 41
    assert body["config_revision"] == grant.config_revision
    assert "settings" in body


_FRONT_DOOR_SECRET = "runtime-config-front-door-secret"
_OPERATOR_IDENTITY = {
    "X-Gobby-Front-Door": _FRONT_DOOR_SECRET,
    "X-Gobby-User-Id": "runtime-config-user",
    "X-Gobby-Machine-Id": LOCAL_MACHINE_ID,
    "X-Gobby-Key-Id": "runtime-config-key",
}
