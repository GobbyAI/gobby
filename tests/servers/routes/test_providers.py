"""Tests for provider availability API routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.config.app import DaemonConfig, LocalConfig
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig
from gobby.servers.routes.providers import create_providers_router

pytestmark = pytest.mark.unit


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    router = create_providers_router()
    app.include_router(router)
    return TestClient(app)


class TestProviderRoutes:
    """Tests for GET /api/providers."""

    def test_list_providers_returns_all_three(self, client: TestClient) -> None:
        """Endpoint returns claude, gemini, and codex entries."""
        response = client.get("/api/providers")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        names = [p["name"] for p in data["providers"]]
        assert names == ["claude", "gemini", "codex"]

    def test_provider_available_when_binary_found(self, client: TestClient) -> None:
        """Provider is marked available when shutil.which finds the binary."""
        with patch("gobby.servers.routes.providers.shutil.which") as mock_which:
            mock_which.side_effect = lambda b: "/usr/bin/claude" if b == "claude" else None
            response = client.get("/api/providers")
            data = response.json()
            providers = {p["name"]: p for p in data["providers"]}
            assert providers["claude"]["available"] is True
            assert providers["claude"]["path"] == "/usr/bin/claude"
            assert providers["gemini"]["available"] is False
            assert providers["gemini"]["path"] is None
            assert providers["codex"]["available"] is False
            assert providers["codex"]["path"] is None

    def test_all_providers_unavailable(self, client: TestClient) -> None:
        """All providers unavailable when no binaries found."""
        with patch("gobby.servers.routes.providers.shutil.which", return_value=None):
            response = client.get("/api/providers")
            data = response.json()
            for p in data["providers"]:
                assert p["available"] is False
                assert p["path"] is None

    def test_all_providers_available(self, client: TestClient) -> None:
        """All providers available when all binaries found."""
        paths = {
            "claude": "/usr/local/bin/claude",
            "gemini": "/usr/local/bin/gemini",
            "codex": "/usr/local/bin/codex",
        }
        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: paths.get(b),
        ):
            response = client.get("/api/providers")
            data = response.json()
            for p in data["providers"]:
                assert p["available"] is True
                assert p["path"] == paths[p["name"]]


class TestProviderModelsRoute:
    """Tests for GET /api/providers/models."""

    def test_returns_all_providers_with_models(self, client: TestClient) -> None:
        """Endpoint returns claude, gemini, and codex with model lists."""
        response = client.get("/api/providers/models")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        providers = {p["provider"]: p for p in data["providers"]}
        assert set(providers.keys()) == {"claude", "gemini", "codex"}

        # Claude should have opus, sonnet, haiku
        claude_values = [m["value"] for m in providers["claude"]["models"]]
        assert claude_values == ["opus", "sonnet", "haiku"]

        # Gemini should expose the hardcoded web-chat defaults
        gemini = providers["gemini"]["models"]
        assert [m["value"] for m in gemini] == ["gemini-3.1-pro", "gemini-3-flash"]
        assert [m["label"] for m in gemini] == ["pro-3.1", "flash-3"]

        # Codex should expose the hardcoded web-chat defaults, not a placeholder
        codex = providers["codex"]["models"]
        assert [m["value"] for m in codex] == [
            "gpt-5.4",
            "gpt-5.3-codex",
            "gpt-5.3-codex-spark",
        ]
        assert [m["label"] for m in codex] == ["codex-5.4", "codex-5.3", "spark-5.3"]

        # Each entry should have source field
        for p in data["providers"]:
            assert p["source"] == "static"

    def test_availability_reflects_binary_presence(self, client: TestClient) -> None:
        """Provider availability matches shutil.which results."""
        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: "/bin/claude" if b == "claude" else None,
        ):
            response = client.get("/api/providers/models")
            providers = {p["provider"]: p for p in response.json()["providers"]}
            assert providers["claude"]["available"] is True
            assert providers["gemini"]["available"] is False
            assert providers["codex"]["available"] is False

    def test_includes_local_claude_model_when_configured(self) -> None:
        """Claude model catalog exposes a local option when daemon local config exists."""
        app = FastAPI()
        config = DaemonConfig(
            local=LocalConfig(url="http://localhost:1234/v1", model="qwen-coder-32b"),
        )
        server = SimpleNamespace(services=SimpleNamespace(config=config))
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")
        providers = {p["provider"]: p for p in response.json()["providers"]}
        claude_models = {m["value"]: m["label"] for m in providers["claude"]["models"]}

        assert claude_models["local"] == "Local (qwen-coder-32b)"

    def test_prefers_configured_codex_and_gemini_models(self) -> None:
        """Configured model lists override the static Gemini/Codex fallback catalog."""
        app = FastAPI()
        config = DaemonConfig(
            llm_providers=LLMProvidersConfig(
                codex=LLMProviderConfig(models="gpt-5.4,gpt-5.3-codex"),
                gemini=LLMProviderConfig(models="gemini-3.1-pro,gemini-3-flash"),
            )
        )
        server = SimpleNamespace(services=SimpleNamespace(config=config))
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")
        providers = {p["provider"]: p for p in response.json()["providers"]}

        assert providers["gemini"]["source"] == "config"
        assert providers["codex"]["source"] == "config"
        assert [m["label"] for m in providers["gemini"]["models"]] == ["pro-3.1", "flash-3"]
        assert [m["label"] for m in providers["codex"]["models"]] == [
            "codex-5.4",
            "codex-5.3",
        ]
