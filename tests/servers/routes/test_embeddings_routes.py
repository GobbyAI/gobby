from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import gobby.servers.routes.embeddings as embeddings_routes
from gobby.ai.embedding_switch_service import SwitchOperationStatus
from gobby.ai.embeddings import EmbeddingService
from gobby.config.app import DaemonConfig
from gobby.config.persistence import EmbeddingsConfig
from gobby.servers.routes.embeddings import create_embeddings_router
from gobby.storage.config_store import EmbeddingConfigMutationBlocked

pytestmark = pytest.mark.unit


def _config(
    *,
    model: str = "nomic-embed-text",
    dim: int = 768,
    api_base: str | None = "http://localhost:1234/v1",
    api_key: str | None = None,
) -> DaemonConfig:
    return DaemonConfig(
        embeddings=EmbeddingsConfig(
            model=model,
            dim=dim,
            api_base=api_base,
            api_key=api_key,
        )
    )


def _client(config: DaemonConfig) -> TestClient:
    server = MagicMock()
    server.config = config
    app = FastAPI()
    app.include_router(create_embeddings_router(server))
    return TestClient(app)


def test_embeddings_status_reports_disabled_config() -> None:
    client = _client(_config(api_base=None, api_key=None))

    response = client.get("/api/embeddings/status")

    assert response.status_code == 200
    data = response.json()
    assert data["embedding_enabled"] is False
    assert data["capability"] == "embed"
    assert data["available"] is False
    assert data["provider"] == "local"
    assert data["model"] == "nomic-embed-text"
    assert data["dim"] == 768
    assert data["reason"]
    assert data["api_base_configured"] is False
    assert data["api_key_configured"] is False


def test_embeddings_status_reports_enabled_config_without_secret() -> None:
    api_key = "sk-test-secret"
    client = _client(
        _config(
            model="text-embedding-3-small",
            dim=1536,
            api_base="http://embeddings.local/v1",
            api_key=api_key,
        )
    )

    response = client.get("/api/embeddings/status")

    assert response.status_code == 200
    data = response.json()
    assert data["embedding_enabled"] is True
    assert data["available"] is True
    assert data["provider"] == "local"
    assert data["model"] == "text-embedding-3-small"
    assert data["dim"] == 1536
    assert data["endpoint"] == "http://embeddings.local/v1"
    assert data["api_base_configured"] is True
    assert data["api_key_configured"] is True
    assert api_key not in json.dumps(data)


def test_embeddings_post_treats_single_input_as_one_item_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_generate_embeddings(
        service: embeddings_routes.EmbeddingService,
        texts: list[str],
        *,
        model: str | None = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
        is_query: bool = False,
    ) -> list[list[float]]:
        calls.append(
            {
                "texts": texts,
                "model": service.model,
                "api_base": service.api_base,
                "api_key": service.api_key,
                "dim": service.dim,
                "requested_model": model,
                "max_retries": max_retries,
                "base_delay": base_delay,
                "is_query": is_query,
            }
        )
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(
        EmbeddingService,
        "generate_embeddings",
        fake_generate_embeddings,
    )
    client = _client(
        _config(
            model="custom-embed",
            dim=3,
            api_base="http://embeddings.local/v1",
            api_key="secret",
        )
    )

    response = client.post("/api/embeddings", json={"input": "alpha"})

    assert response.status_code == 200
    assert response.json() == {"embeddings": [[0.1, 0.2, 0.3]], "model": "custom-embed", "dim": 3}
    assert calls == [
        {
            "texts": ["alpha"],
            "model": "custom-embed",
            "api_base": "http://embeddings.local/v1",
            "api_key": "secret",
            "dim": 3,
            "requested_model": None,
            "max_retries": 5,
            "base_delay": 1.0,
            "is_query": False,
        }
    ]


def test_embeddings_payload_schema_documents_reserved_routing_fields() -> None:
    schema = embeddings_routes.EmbeddingsPayload.model_json_schema()

    properties = schema["properties"]
    assert properties["provider"]["description"].startswith("Reserved for future provider routing")
    assert properties["project_id"]["description"].startswith(
        "Reserved for future multi-project embedding routing"
    )


def test_embeddings_post_preserves_batch_order_and_passes_is_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_generate_embeddings(
        service: embeddings_routes.EmbeddingService,
        texts: list[str],
        *,
        model: str | None = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
        is_query: bool = False,
    ) -> list[list[float]]:
        calls.append(
            {
                "texts": texts,
                "dim": service.dim,
                "model": model,
                "max_retries": max_retries,
                "base_delay": base_delay,
                "is_query": is_query,
            }
        )
        return [[1.0], [2.0]]

    monkeypatch.setattr(
        EmbeddingService,
        "generate_embeddings",
        fake_generate_embeddings,
    )
    client = _client(_config(dim=1, api_base="http://embeddings.local/v1"))

    response = client.post(
        "/api/embeddings",
        json={"input": ["first", "second"], "is_query": True},
    )

    assert response.status_code == 200
    assert response.json()["embeddings"] == [[1.0], [2.0]]
    assert calls[0]["texts"] == ["first", "second"]
    assert calls[0]["is_query"] is True
    assert calls[0]["model"] is None
    assert calls[0]["dim"] == 1


def test_embeddings_doctor_returns_endpoint_model_and_dim() -> None:
    client = _client(
        _config(
            model="bge-m3",
            dim=1024,
            api_base="http://embeddings.local/v1",
        )
    )

    response = client.get("/api/embeddings/doctor")

    assert response.status_code == 200
    assert response.json() == {
        "endpoint": "http://embeddings.local/v1",
        "model": "bge-m3",
        "dim": 1024,
    }


def test_embedding_switch_routes_delegate_to_daemon_coordinator() -> None:
    calls: list[tuple[str, object, object]] = []

    class Coordinator:
        def status(self) -> SwitchOperationStatus:
            return SwitchOperationStatus("run-1", "running", "in progress")

        async def start(
            self,
            catalog_key: str,
            provider: str | None,
            api_base: str | None = None,
        ) -> SwitchOperationStatus:
            calls.append((catalog_key, provider, api_base))
            return SwitchOperationStatus("run-1", "started", "started")

        async def resume(self) -> SwitchOperationStatus:
            return SwitchOperationStatus("run-1", "resumed", "resumed")

        async def abort(self) -> SwitchOperationStatus:
            return SwitchOperationStatus("run-1", "aborted", "aborted")

    server = MagicMock()
    server.config = _config()
    server.get_runner.return_value = SimpleNamespace(embedding_switch_coordinator=Coordinator())
    app = FastAPI()
    app.include_router(create_embeddings_router(server))
    client = TestClient(app)

    start = client.post(
        "/api/embeddings/switch/start",
        json={"catalog_key": "qwen3-8b-q8", "provider": "ollama"},
    )
    status = client.get("/api/embeddings/switch/status")
    resume = client.post("/api/embeddings/switch/resume")
    abort = client.post("/api/embeddings/switch/abort")

    assert start.json()["status"] == "started"
    assert status.json()["status"] == "running"
    assert resume.json()["status"] == "resumed"
    assert abort.json()["status"] == "aborted"
    assert calls == [("qwen3-8b-q8", "ollama", None)]


def test_embedding_switch_start_forwards_api_base_for_vllm() -> None:
    calls: list[tuple[str, object, object]] = []

    class Coordinator:
        async def start(
            self,
            catalog_key: str,
            provider: str | None,
            api_base: str | None = None,
        ) -> SwitchOperationStatus:
            calls.append((catalog_key, provider, api_base))
            return SwitchOperationStatus("run-1", "started", "started")

    server = MagicMock()
    server.config = _config()
    server.get_runner.return_value = SimpleNamespace(embedding_switch_coordinator=Coordinator())
    app = FastAPI()
    app.include_router(create_embeddings_router(server))

    response = TestClient(app).post(
        "/api/embeddings/switch/start",
        json={
            "catalog_key": "qwen3-0.6b-q8",
            "provider": "vllm",
            "api_base": "http://localhost:8323/v1",
        },
    )

    assert response.status_code == 200
    assert calls == [("qwen3-0.6b-q8", "vllm", "http://localhost:8323/v1")]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/api/embeddings/switch/status", None),
        (
            "POST",
            "/api/embeddings/switch/start",
            {"catalog_key": "qwen3-8b-q8", "provider": "ollama"},
        ),
        ("POST", "/api/embeddings/switch/resume", None),
        ("POST", "/api/embeddings/switch/abort", None),
    ],
)
def test_embedding_switch_routes_report_unavailable_coordinator(
    method: str,
    path: str,
    payload: dict[str, str] | None,
) -> None:
    server = MagicMock()
    server.config = _config()
    server.get_runner.return_value = SimpleNamespace()
    app = FastAPI()
    app.include_router(create_embeddings_router(server))
    client = TestClient(app)

    response = client.request(method, path, json=payload)

    assert response.status_code == 503
    assert response.json()["detail"] == "Embedding switch service is unavailable"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/embeddings/switch/start",
            {"catalog_key": "qwen3-8b-q8", "provider": "ollama"},
        ),
        ("/api/embeddings/switch/resume", None),
        ("/api/embeddings/switch/abort", None),
    ],
)
def test_embedding_switch_mutation_contention_returns_conflict(
    path: str,
    payload: dict[str, str] | None,
) -> None:
    class ContendedCoordinator:
        async def start(
            self,
            _catalog_key: str,
            _provider: str | None,
            _api_base: str | None = None,
        ) -> SwitchOperationStatus:
            raise EmbeddingConfigMutationBlocked("switch journal is locked")

        async def resume(self) -> SwitchOperationStatus:
            raise EmbeddingConfigMutationBlocked("switch journal is locked")

        async def abort(self) -> SwitchOperationStatus:
            raise EmbeddingConfigMutationBlocked("switch journal is locked")

    server = MagicMock()
    server.config = _config()
    server.get_runner.return_value = SimpleNamespace(
        embedding_switch_coordinator=ContendedCoordinator()
    )
    app = FastAPI()
    app.include_router(create_embeddings_router(server))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(path, json=payload)

    assert response.status_code == 409
    assert response.json()["detail"] == "switch journal is locked"
