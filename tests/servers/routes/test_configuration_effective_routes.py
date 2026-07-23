"""Tests for the daemon-served effective configuration endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.servers.auth_service import AuthService
from gobby.storage.auth import LOCAL_API_TOKEN_HASH_KEY, AuthStore, hash_token
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

LOCAL_RUNTIME_TOKEN = "effective-config-test-token"


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


@pytest.fixture
def server(
    hub_db: HubDatabase,
    runtime_config: DaemonConfig,
    tmp_path: Path,
    mock_machine_id: Any,
) -> Any:
    ConfigStore(hub_db).set(
        LOCAL_API_TOKEN_HASH_KEY,
        hash_token(LOCAL_RUNTIME_TOKEN),
        source="system",
    )
    http_server = create_http_server(
        config=runtime_config,
        database=hub_db,
        task_manager=LocalTaskManager(hub_db),
        auth_mode="disabled",
    )
    http_server.auth_service = AuthService(
        lambda: hub_db,
        mode="disabled",
        token_file=tmp_path / "missing-local-token",
    )
    return http_server


@pytest.fixture
def client(server: Any) -> TestClient:
    return TestClient(
        server.app,
        headers={"X-Gobby-Local-Token": LOCAL_RUNTIME_TOKEN},
    )


def test_effective_config_filters_resolves_stringifies_and_overlays(
    client: TestClient,
    hub_db: HubDatabase,
    runtime_config: DaemonConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ConfigStore(hub_db)
    secret_store = SecretStore(hub_db)
    candidates = '[{"provider":"lmstudio","model":"candidate-model"}]'
    monkeypatch.setenv("EFFECTIVE_CONFIG_ENV", "daemon-environment")

    store.set_many(
        {
            "ai.embeddings.model": "stale-store-model",
            "ai.embeddings.query_prefix": "stale-query-prefix",
            "ai.keep_alive": True,
            "ai.routing": "store-routing",
            "ai.text_generate.routing": "store-text-routing",
            "ai.text_generate.candidates": candidates,
            "databases.falkordb.host": "stale-falkor",
            "databases.falkordb.port": 6379,
            "databases.qdrant.url": "http://stale-qdrant.test:6333",
            "indexing.batch_size": 32,
            "gwiki.enabled": False,
            "gwiki.environment": "${EFFECTIVE_CONFIG_ENV}",
            "gwiki.unresolved": "${EFFECTIVE_CONFIG_MISSING}",
            "unrelated.value": "must-not-be-served",
        }
    )
    store.set_secret(
        "ai.text_generate.api_key",
        "resolved-text-generation-key",
        secret_store,
    )
    store.set(
        "ai.text_generate.missing_secret",
        "$secret:missing_secret",
    )
    store.set_secret(
        "ai.text_generate.broken_secret",
        "value-that-will-become-undecryptable",
        secret_store,
    )
    store.set_secret(
        "databases.falkordb.password",
        "stale-falkor-password",
        secret_store,
    )
    hub_db.execute(
        "UPDATE secrets SET encrypted_value = %s WHERE name = %s",
        ("not-a-valid-fernet-token", "broken_secret"),
    )
    store.set_internal_lifecycle(
        "ai.embeddings.switch_run",
        {"run_id": "effective-config-test-run"},
    )

    response = client.get("/api/config/effective")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert set(response.json()) == {"config"}
    values = response.json()["config"]
    assert values["ai.embeddings.model"] == "daemon-embedding-model"
    assert values["ai.embeddings.dim"] == "768"
    assert values["ai.embeddings.api_base"] == "http://daemon-embeddings.test/v1"
    assert values["ai.embeddings.api_key"] == "daemon-embedding-key"
    assert values["ai.embeddings.catalog_key"] == "daemon-catalog"
    assert values["ai.keep_alive"] == "true"
    assert values["ai.text_generate.api_key"] == "resolved-text-generation-key"
    assert values["ai.text_generate.candidates"] == candidates
    assert values["databases.falkordb.host"] == "daemon-falkor.test"
    assert values["databases.falkordb.port"] == "16379"
    assert values["databases.qdrant.url"] == "http://daemon-qdrant.test:6333"
    assert values["databases.qdrant.api_key"] == "daemon-qdrant-key"
    assert values["databases.postgres.dsn"] == runtime_config.database_url
    assert values["indexing.batch_size"] == "32"
    assert values["gwiki.enabled"] == "false"
    assert values["gwiki.environment"] == "daemon-environment"

    assert "ai.embeddings.query_prefix" not in values
    assert "databases.falkordb.password" not in values
    assert "ai.text_generate.missing_secret" not in values
    assert "ai.text_generate.broken_secret" not in values
    assert "gwiki.unresolved" not in values
    assert "ai.routing" not in values
    assert "ai.text_generate.routing" not in values
    assert "ai.embeddings.switch_run" not in values
    assert "unrelated.value" not in values
    assert all("$secret:" not in value and "${" not in value for value in values.values())


def test_post_overlay_markers_are_omitted(
    client: TestClient,
    server: Any,
) -> None:
    runtime_config = server.services.config
    server.services.config = runtime_config.model_copy(
        update={
            "embeddings": runtime_config.embeddings.model_copy(
                update={
                    "api_key": "$secret:must-not-escape",
                    "api_base": "${UNRESOLVED_OVERLAY_VALUE}",
                }
            ),
            "databases": runtime_config.databases.model_copy(
                update={
                    "qdrant": runtime_config.databases.qdrant.model_copy(
                        update={"api_key": "$secret:must-not-escape"}
                    )
                }
            ),
        }
    )

    response = client.get("/api/config/effective")

    assert response.status_code == 200
    values = response.json()["config"]
    assert "ai.embeddings.api_key" not in values
    assert "ai.embeddings.api_base" not in values
    assert "databases.qdrant.api_key" not in values
    assert all("$secret:" not in value and "${" not in value for value in values.values())


def test_effective_config_requires_runtime_token_even_when_auth_is_disabled(
    server: Any,
    hub_db: HubDatabase,
) -> None:
    unauthenticated = TestClient(server.app)
    cookie_client = TestClient(server.app)
    session_token, _ = AuthStore(hub_db).create_session()

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

    assert server.auth_service.enabled is False
    assert no_credentials.status_code == 401
    assert cookie_only.status_code == 401
    assert invalid_bearer.status_code == 401
    assert bearer.status_code == 200
    assert local_header.status_code == 200


def test_effective_config_returns_503_when_runtime_config_is_unavailable(
    client: TestClient,
    server: Any,
) -> None:
    server.services.config = None

    response = client.get("/api/config/effective")

    assert response.status_code == 503
