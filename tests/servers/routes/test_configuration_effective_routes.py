"""Tests for the daemon-served effective configuration endpoint."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.config.runtime import ConfigRuntime, ConfigSnapshot, RuntimeSecretBinding
from gobby.servers.auth_service import AuthService
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.tasks import LocalTaskManager
from tests.fixtures.postgres import TEST_USER_ID
from tests.servers.conftest import create_http_server

LOCAL_RUNTIME_TOKEN = "effective-config-test-token"

LOCAL_MACHINE_ID = "22000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity(hub_db: HubDatabase) -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        LocalMachineManager(hub_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID)
        yield


@pytest.fixture
def runtime_config() -> DaemonConfig:
    return DaemonConfig(
        database_url="postgresql://daemon:gobby@127.0.0.1:5432/gobby",
        embeddings={
            "model": "daemon-embedding-model",
            "dim": 768,
            "api_base": "http://daemon-embeddings.test/v1",
            "api_key": "daemon-embedding-key",
            "query_prefix": None,
            "catalog_key": "daemon-catalog",
        },
        databases={
            "falkordb": {
                "host": "daemon-falkor.test",
                "port": 16379,
                "password": None,
            },
            "qdrant": {
                "url": "http://daemon-qdrant.test:6333",
                "api_key": "daemon-qdrant-key",
            },
        },
    )


def _machine_values(config: DaemonConfig) -> dict[str, object]:
    return {
        "ai.embeddings.model": config.embeddings.model,
        "ai.embeddings.dim": config.embeddings.dim,
        "ai.embeddings.api_key": "$secret:test-embedding-key",
        "ai.embeddings.query_prefix": config.embeddings.query_prefix,
        "ai.embeddings.routing": "daemon",
        "databases.falkordb.host": config.databases.falkordb.host,
        "databases.falkordb.port": config.databases.falkordb.port,
        "databases.qdrant.url": config.databases.qdrant.url,
        "indexing.respect_gitignore": config.indexing.respect_gitignore,
    }


def _snapshot(
    active: DaemonConfig,
    *,
    active_values: Mapping[str, object],
    desired: DaemonConfig | None = None,
    desired_values: Mapping[str, object] | None = None,
    desired_secrets: Mapping[str, str | None] | None = None,
    active_secrets: Mapping[str, str | None] | None = None,
) -> ConfigSnapshot:
    desired_projection = desired or active
    desired_projection_values = dict(desired_values or active_values)
    active_projection_values = dict(active_values)
    desired_bindings = {
        key: RuntimeSecretBinding(str(desired_projection_values[key]), value, f"desired-{key}")
        for key, value in (desired_secrets or {}).items()
    }
    active_bindings = {
        key: RuntimeSecretBinding(str(active_projection_values[key]), value, f"active-{key}")
        for key, value in (active_secrets or {}).items()
    }
    return ConfigSnapshot(
        revision=7,
        desired=desired_projection,
        active=active,
        row_revisions=dict.fromkeys(desired_projection_values, 7),
        pending_restart_keys=frozenset(),
        failed_live_keys={},
        desired_values=desired_projection_values,
        active_values=active_projection_values,
        desired_bindings=desired_bindings,
        active_bindings=active_bindings,
    )


@pytest.fixture
def server(
    hub_db: HubDatabase,
    runtime_config: DaemonConfig,
    tmp_path: Path,
) -> Any:
    AuthStore(hub_db).set_local_api_token_hash(
        hash_token(LOCAL_RUNTIME_TOKEN),
    )
    http_server = create_http_server(
        config=runtime_config,
        database=hub_db,
        task_manager=LocalTaskManager(hub_db),
    )
    token_file = tmp_path / "local-cli-token"
    token_file.write_text(LOCAL_RUNTIME_TOKEN, encoding="utf-8")
    http_server.auth_service = AuthService(
        lambda: hub_db,
        token_file=token_file,
    )
    runtime = MagicMock(spec=ConfigRuntime)
    runtime.snapshot = _snapshot(
        runtime_config,
        active_values=_machine_values(runtime_config),
        active_secrets={"ai.embeddings.api_key": "daemon-embedding-key"},
    )
    http_server.services.config_runtime = runtime
    return http_server


@pytest.fixture
def client(server: Any) -> TestClient:
    return TestClient(
        server.app,
        headers={"X-Gobby-Local-Token": LOCAL_RUNTIME_TOKEN},
    )


def test_service_capabilities_route_is_gone(client: TestClient) -> None:
    response = client.get("/api/config/service-capabilities")
    assert response.status_code == 404


@pytest.mark.integration
def test_effective_config_never_serves_secrets_in_plaintext(
    client: TestClient,
    server: Any,
    runtime_config: DaemonConfig,
) -> None:
    """Configured, resolvable secrets must never appear in the machine export."""
    active = runtime_config.model_copy(deep=True)
    active.embeddings.model = "active-snapshot-model"
    active.embeddings.api_key = "$secret:active-embedding-key"
    active.databases.falkordb.password = "$secret:active-falkordb-password"
    values = {
        "ai.embeddings.model": "active-snapshot-model",
        "ai.embeddings.api_key": "$secret:active-embedding-key",
        "databases.falkordb.password": "$secret:active-falkordb-password",
    }
    server.services.config_runtime.snapshot = _snapshot(
        active,
        active_values=values,
        desired_secrets={
            "ai.embeddings.api_key": "resolved-active-key",
            "databases.falkordb.password": "resolved-falkordb-password",
        },
        active_secrets={
            "ai.embeddings.api_key": "resolved-active-key",
            "databases.falkordb.password": "resolved-falkordb-password",
        },
    )

    response = client.get("/api/config/effective")

    assert response.status_code == 200
    assert set(response.json()) == {"revision", "config"}
    assert response.json()["revision"] == 7
    assert response.json()["config"]["ai.embeddings.model"] == "active-snapshot-model"
    assert "ai.embeddings.api_key" not in response.json()["config"]
    assert "databases.falkordb.password" not in response.json()["config"]
    assert "resolved-active-key" not in response.text
    assert "resolved-falkordb-password" not in response.text
    assert "$secret:" not in response.text


@pytest.mark.integration
def test_effective_config_uses_machine_visibility(
    client: TestClient,
    server: Any,
    runtime_config: DaemonConfig,
) -> None:
    server.services.config_runtime.snapshot = _snapshot(
        runtime_config,
        active_values={
            "ai.embeddings.model": "machine-visible",
            "websocket.ping_interval": 17.0,
            "auth.api_token_hash": "restricted-value",
        },
    )

    response = client.get("/api/config/effective")

    assert response.status_code == 200
    assert response.json() == {
        "revision": 7,
        "config": {"ai.embeddings.model": "machine-visible"},
    }


def test_machine_output_leaks_neither_secret_binding(
    client: TestClient,
    server: Any,
    runtime_config: DaemonConfig,
) -> None:
    """Neither the active nor the desired secret payload may reach the export."""
    key = "ai.embeddings.api_key"
    desired = runtime_config.model_copy(deep=True)
    desired.embeddings.api_key = "$secret:rotated-key"
    active = runtime_config.model_copy(deep=True)
    active.embeddings.api_key = "$secret:activated-key"
    server.services.config_runtime.snapshot = _snapshot(
        active,
        desired=desired,
        desired_values={key: "$secret:rotated-key"},
        active_values={key: "$secret:activated-key"},
        desired_secrets={key: "rotated-unactivated-payload"},
        active_secrets={key: "activated-payload"},
    )

    response = client.get("/api/config/effective")

    assert response.status_code == 200
    assert response.json() == {"revision": 7, "config": {}}
    assert "activated-payload" not in response.text
    assert "rotated-unactivated-payload" not in response.text
    assert "$secret:" not in response.text


def test_post_overlay_markers_are_omitted(
    client: TestClient,
    server: Any,
    runtime_config: DaemonConfig,
) -> None:
    server.services.config_runtime.snapshot = _snapshot(
        runtime_config,
        active_values={
            "ai.embeddings.api_key": "$secret:must-not-escape",
            "ai.embeddings.api_base": "${UNRESOLVED_OVERLAY_VALUE}",
            "databases.qdrant.api_key": "$secret:must-not-escape",
        },
    )

    response = client.get("/api/config/effective")

    assert response.status_code == 200
    values = response.json()["config"]
    assert "ai.embeddings.api_key" not in values
    assert "ai.embeddings.api_base" not in values
    assert "databases.qdrant.api_key" not in values
    assert all("$secret:" not in value and "${" not in value for value in values.values())


@pytest.mark.integration
def test_effective_config_auth_and_cache_contract(
    server: Any,
    hub_db: HubDatabase,
) -> None:
    unauthenticated = TestClient(server.app)
    cookie_client = TestClient(server.app)
    session_token, _ = AuthStore(hub_db).create_session(TEST_USER_ID)

    no_credentials = unauthenticated.get("/api/config/effective")
    cookie_client.cookies.set("gobby_session", session_token)
    cookie_only = cookie_client.get("/api/config/effective")
    invalid_bearer = unauthenticated.get(
        "/api/config/effective",
        headers={"Authorization": "Bearer invalid-token"},
    )
    bearer = unauthenticated.get(
        "/api/config/effective",
        headers={"Authorization": f"Bearer {LOCAL_RUNTIME_TOKEN}"},
    )
    local_header = unauthenticated.get(
        "/api/config/effective",
        headers={"X-Gobby-Local-Token": LOCAL_RUNTIME_TOKEN},
    )

    assert no_credentials.status_code == 401
    assert cookie_only.status_code == 401
    assert invalid_bearer.status_code == 401
    assert bearer.status_code == 200
    assert local_header.status_code == 200
    assert bearer.headers["Cache-Control"] == "no-store"
    assert local_header.headers["Cache-Control"] == "no-store"


@pytest.mark.integration
def test_effective_config_returns_503_when_runtime_config_is_unavailable(
    client: TestClient,
    server: Any,
) -> None:
    server.services.config_runtime = None

    response = client.get("/api/config/effective")

    assert response.status_code == 503


def test_effective_config_reads_return_retryable_503_during_startup(
    client: TestClient,
    server: Any,
) -> None:
    runtime = server.services.config_runtime
    type(runtime).snapshot = PropertyMock(side_effect=RuntimeError("ConfigRuntime has not started"))

    response = client.get("/api/config/effective")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"
    assert response.json()["error"]["retryable"] is True
