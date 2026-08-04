"""Tests for the daemon-served effective configuration endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.servers.auth_service import AuthService
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.auth import LOCAL_API_TOKEN_HASH_KEY, AuthStore, hash_token
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.local_token import AgentApiTokenClaims, issue_agent_api_token
from tests.servers.conftest import create_http_server

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager

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
    token_file = tmp_path / "local-cli-token"
    token_file.write_text(LOCAL_RUNTIME_TOKEN, encoding="utf-8")
    http_server.auth_service = AuthService(
        lambda: hub_db,
        mode="disabled",
        token_file=token_file,
    )
    return http_server


@pytest.fixture
def client(server: Any) -> TestClient:
    return TestClient(
        server.app,
        headers={"X-Gobby-Local-Token": LOCAL_RUNTIME_TOKEN},
    )


def _agent_headers(
    hub_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    *,
    minted_at: float | None = None,
) -> tuple[AgentRun, dict[str, str]]:
    session = session_manager.register(
        external_id="service-capabilities-agent",
        machine_id="22000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    run = LocalAgentRunManager(hub_db).create(
        parent_session_id=session.id,
        provider="claude",
        prompt="service capabilities",
    )
    token_args = {
        "agent_run_id": run.id,
        "session_id": session.id,
        "project_id": sample_project["id"],
    }
    if minted_at is None:
        token = issue_agent_api_token(LOCAL_RUNTIME_TOKEN, **token_args)
    else:
        with patch("gobby.utils.local_token.time.time", return_value=minted_at):
            token = issue_agent_api_token(LOCAL_RUNTIME_TOKEN, **token_args)
    return run, {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Agent-Run-Id": run.id,
        "X-Gobby-Caller-Project-Id": sample_project["id"],
        "X-Gobby-Session-Id": session.id,
    }


@pytest.mark.integration
def test_service_capabilities_are_claim_bound_and_allowlisted(
    server: Any,
    hub_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    ConfigStore(hub_db).set_many(
        {
            "indexing.respect_gitignore": False,
            "gwiki.enabled": True,
        }
    )
    run, headers = _agent_headers(hub_db, session_manager, sample_project)

    response = TestClient(server.app).get("/api/config/service-capabilities", headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    assert payload["version"] == 1
    assert payload["execution"] == {
        "owner_kind": "agent_run",
        "execution_id": run.id,
        "project_id": sample_project["id"],
        "session_id": headers["X-Gobby-Session-Id"],
        "expires_at": payload["execution"]["expires_at"],
    }
    assert payload["config"] == {
        "ai.embeddings.dim": "768",
        "ai.embeddings.model": "daemon-embedding-model",
        "ai.embeddings.routing": "daemon",
        "databases.falkordb.host": "daemon-falkor.test",
        "databases.falkordb.port": "16379",
        "indexing.respect_gitignore": "false",
    }
    assert payload["services"] == {
        "embeddings": {
            "mode": "brokered",
            "operations": [{"name": "embed", "method": "POST", "path": "/api/embeddings"}],
        },
        "falkordb": {
            "mode": "direct",
            "operations": [
                {
                    "name": "clear_projection",
                    "method": "POST",
                    "path": "/api/code-index/graph/clear",
                },
                {
                    "name": "rebuild_projection",
                    "method": "POST",
                    "path": "/api/code-index/graph/rebuild",
                },
            ],
        },
        "qdrant": {
            "mode": "brokered",
            "operations": [
                {
                    "name": "invalidate_projection",
                    "method": "POST",
                    "path": "/api/code-index/invalidate",
                }
            ],
        },
    }
    for forbidden in (
        "daemon-embedding-key",
        "daemon-qdrant-key",
        "postgresql://daemon",
        ".secret_kek",
        "gwiki.enabled",
    ):
        assert forbidden not in response.text


def test_service_capabilities_bind_managed_tool_execution(
    server: Any,
    client: TestClient,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = "11111111-2222-4333-8444-555555555555"
    session_id = "22222222-3333-4444-8555-666666666666"
    claims = AgentApiTokenClaims(
        session_id=session_id,
        project_id=sample_project["id"],
        iat=1,
        exp=4_102_444_800,
        managed_execution_id=execution_id,
    )
    monkeypatch.setattr(
        server.auth_service,
        "verified_agent_claims",
        lambda _request: claims,
    )

    response = client.get("/api/config/service-capabilities")

    assert response.status_code == 200, response.text
    assert response.json()["execution"] == {
        "owner_kind": "tool_chat",
        "execution_id": execution_id,
        "project_id": sample_project["id"],
        "session_id": session_id,
        "expires_at": 4_102_444_800,
    }


@pytest.mark.integration
def test_service_capabilities_reject_operator_expired_and_mismatched_identity(
    server: Any,
    hub_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    _, valid_headers = _agent_headers(hub_db, session_manager, sample_project)
    _, expired_headers = _agent_headers(
        hub_db,
        session_manager,
        sample_project,
        minted_at=1,
    )
    operator_headers = {
        **valid_headers,
        "Authorization": f"Bearer {LOCAL_RUNTIME_TOKEN}",
    }
    mismatches = [
        {**valid_headers, "X-Gobby-Agent-Run-Id": "wrong-run"},
        {**valid_headers, "X-Gobby-Caller-Project-Id": "wrong-project"},
        {**valid_headers, "X-Gobby-Session-Id": "wrong-session"},
    ]
    client = TestClient(server.app)

    responses = [
        client.get("/api/config/service-capabilities", headers=operator_headers),
        client.get("/api/config/service-capabilities", headers=expired_headers),
        *(
            client.get("/api/config/service-capabilities", headers=headers)
            for headers in mismatches
        ),
    ]

    assert [response.status_code for response in responses] == [401, 401, 401, 401, 401]


@pytest.mark.integration
def test_service_capabilities_reject_query_selected_secrets(
    server: Any,
    hub_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    _, headers = _agent_headers(hub_db, session_manager, sample_project)

    response = TestClient(server.app).get(
        "/api/config/service-capabilities?secret_name=embedding_api_key",
        headers=headers,
    )

    assert response.status_code == 400


@pytest.mark.integration
@pytest.mark.parametrize(
    "service_url",
    ["http://shared:credential@daemon-qdrant.test:6333", "not a valid URL"],
)
def test_service_capabilities_broker_credentialed_or_invalid_service_urls(
    server: Any,
    hub_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    service_url: str,
) -> None:
    config = server.services.config
    server.services.config = config.model_copy(
        update={
            "databases": config.databases.model_copy(
                update={
                    "qdrant": config.databases.qdrant.model_copy(
                        update={
                            "url": service_url,
                            "api_key": None,
                        }
                    )
                }
            )
        }
    )
    _, headers = _agent_headers(hub_db, session_manager, sample_project)

    response = TestClient(server.app).get("/api/config/service-capabilities", headers=headers)

    assert response.status_code == 200, response.text
    assert "databases.qdrant.url" not in response.json()["config"]
    assert response.json()["services"]["qdrant"]["mode"] == "brokered"
    assert service_url not in response.text


def test_service_capabilities_omit_unresolved_runtime_config_markers(
    server: Any,
    hub_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    config = server.services.config
    server.services.config = config.model_copy(
        update={
            "embeddings": config.embeddings.model_copy(
                update={"model": "$secret:SHARED_MODEL", "query_prefix": "${SHARED_PREFIX}"}
            ),
            "databases": config.databases.model_copy(
                update={
                    "falkordb": config.databases.falkordb.model_copy(
                        update={"host": "$secret:FALKOR_HOST"}
                    ),
                    "qdrant": config.databases.qdrant.model_copy(
                        update={"url": "http://${SHARED_HOST}:6333", "api_key": None}
                    ),
                }
            ),
        }
    )
    _, headers = _agent_headers(hub_db, session_manager, sample_project)

    response = TestClient(server.app).get("/api/config/service-capabilities", headers=headers)

    assert response.status_code == 200, response.text
    managed_config = response.json()["config"]
    assert "ai.embeddings.model" not in managed_config
    assert "ai.embeddings.query_prefix" not in managed_config
    assert "databases.falkordb.host" not in managed_config
    assert "databases.qdrant.url" not in managed_config
    assert response.json()["services"]["falkordb"]["mode"] == "brokered"
    assert response.json()["services"]["qdrant"]["mode"] == "brokered"
    assert "$secret:" not in response.text
    assert "${" not in response.text


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
def test_effective_config_returns_503_when_runtime_config_is_unavailable(
    client: TestClient,
    server: Any,
) -> None:
    server.services.config = None

    response = client.get("/api/config/effective")

    assert response.status_code == 503
