from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import gobby.servers.routes.embeddings as embeddings_routes
from gobby.config.app import DaemonConfig
from gobby.config.persistence import EmbeddingsConfig
from gobby.servers.routes.embeddings import create_embeddings_router

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

    async def fake_generate_embeddings(texts: list[str], **kwargs: Any) -> list[list[float]]:
        calls.append({"texts": texts, **kwargs})
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(
        embeddings_routes.embeddings_module,
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
            "is_query": False,
            "expected_dim": 3,
        }
    ]


def test_embeddings_post_preserves_batch_order_and_passes_is_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_generate_embeddings(texts: list[str], **kwargs: Any) -> list[list[float]]:
        calls.append({"texts": texts, **kwargs})
        return [[1.0], [2.0]]

    monkeypatch.setattr(
        embeddings_routes.embeddings_module,
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
    assert calls[0]["expected_dim"] == 1


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
