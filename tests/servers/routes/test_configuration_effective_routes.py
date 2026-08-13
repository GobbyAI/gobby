"""Tests for the daemon-served effective configuration endpoint."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.config.runtime import ConfigRuntime, ConfigSnapshot, RuntimeSecretBinding
from gobby.servers.auth_service import AuthService
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.local_token import AgentApiTokenClaims, issue_agent_api_token
from tests.fixtures.postgres import TEST_USER_ID
from tests.servers.conftest import create_http_server

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager

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
    runtime_config: DaemonConfig,
) -> None:
    config = runtime_config
    active_values = _machine_values(config)
    active_values["indexing.respect_gitignore"] = False
    server.services.config_runtime.snapshot = _snapshot(
        config,
        active_values=active_values,
        active_secrets={"ai.embeddings.api_key": "daemon-embedding-key"},
    )
    run, headers = _agent_headers(hub_db, session_manager, sample_project)

    response = TestClient(server.app).get("/api/config/service-capabilities", headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    assert payload["version"] == 1
    assert payload["revision"] == 7
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


def test_service_capabilities_use_active_snapshot(
    server: Any,
    hub_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    runtime_config: DaemonConfig,
) -> None:
    server.services.config = runtime_config.model_copy(
        update={
            "databases": runtime_config.databases.model_copy(
                update={
                    "falkordb": runtime_config.databases.falkordb.model_copy(
                        update={"password": None}
                    )
                }
            )
        }
    )
    active = runtime_config.model_copy(
        update={
            "databases": runtime_config.databases.model_copy(
                update={
                    "falkordb": runtime_config.databases.falkordb.model_copy(
                        update={"password": "$secret:active-falkor-password"}
                    )
                }
            )
        }
    )
    server.services.config_runtime.snapshot = _snapshot(
        active,
        active_values=_machine_values(active),
        active_secrets={"ai.embeddings.api_key": "daemon-embedding-key"},
    )
    _, headers = _agent_headers(hub_db, session_manager, sample_project)

    unauthorized = TestClient(server.app).get("/api/config/service-capabilities")
    response = TestClient(server.app).get("/api/config/service-capabilities", headers=headers)

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["services"]["falkordb"]["mode"] == "brokered"
    assert "databases.falkordb.host" not in response.json()["config"]


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
    runtime_config: DaemonConfig,
) -> None:
    config = runtime_config
    active = config.model_copy(
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
    server.services.config_runtime.snapshot = _snapshot(
        active,
        active_values=_machine_values(active),
    )
    _, headers = _agent_headers(hub_db, session_manager, sample_project)

    response = TestClient(server.app).get("/api/config/service-capabilities", headers=headers)

    assert response.status_code == 200, response.text
    assert "databases.qdrant.url" not in response.json()["config"]
    assert response.json()["services"]["qdrant"]["mode"] == "brokered"
    assert service_url not in response.text


def test_unresolved_bound_secrets_keep_service_capabilities_brokered(
    server: Any,
    hub_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    runtime_config: DaemonConfig,
) -> None:
    active_values = _machine_values(runtime_config)
    active_values.update(
        {
            "databases.falkordb.password": "$secret:missing-falkordb-password",
            "databases.qdrant.api_key": "$secret:missing-qdrant-api-key",
        }
    )
    server.services.config_runtime.snapshot = _snapshot(
        runtime_config,
        active_values=active_values,
        active_secrets={
            "databases.falkordb.password": None,
            "databases.qdrant.api_key": None,
        },
    )
    _, headers = _agent_headers(hub_db, session_manager, sample_project)

    response = TestClient(server.app).get("/api/config/service-capabilities", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["services"]["falkordb"]["mode"] == "brokered"
    assert body["services"]["qdrant"]["mode"] == "brokered"
    assert "databases.falkordb.host" not in body["config"]
    assert "databases.falkordb.port" not in body["config"]
    assert "databases.qdrant.url" not in body["config"]
    assert "$secret:" not in response.text


def test_service_capabilities_omit_unresolved_runtime_config_markers(
    server: Any,
    hub_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    runtime_config: DaemonConfig,
) -> None:
    config = runtime_config
    active = config.model_copy(
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
    server.services.config_runtime.snapshot = _snapshot(
        active,
        active_values=_machine_values(active),
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
    hub_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    runtime = server.services.config_runtime
    type(runtime).snapshot = PropertyMock(side_effect=RuntimeError("ConfigRuntime has not started"))
    _run, agent_headers = _agent_headers(hub_db, session_manager, sample_project)

    responses = (
        client.get("/api/config/effective"),
        client.get("/api/config/service-capabilities", headers=agent_headers),
    )

    for response in responses:
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "runtime_unavailable"
        assert response.json()["error"]["retryable"] is True
