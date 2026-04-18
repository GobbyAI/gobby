"""Tests for provider availability API routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

    def test_list_providers_returns_all_four(self, client: TestClient) -> None:
        """Endpoint returns claude, gemini, qwen, and codex entries."""
        response = client.get("/api/providers")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        names = [p["name"] for p in data["providers"]]
        assert names == ["claude", "gemini", "qwen", "codex"]

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
            assert providers["qwen"]["available"] is False
            assert providers["qwen"]["path"] is None
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
            "qwen": "/usr/local/bin/qwen",
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

    def test_runtime_health_can_disable_startup_failed_provider(self) -> None:
        app = FastAPI()
        runtime_manager = MagicMock()
        runtime_manager.health.side_effect = lambda provider: SimpleNamespace(
            available=False if provider == "gemini" else True,
            startup_error="gemini failed" if provider == "gemini" else None,
        )
        server = SimpleNamespace(
            services=SimpleNamespace(
                config=DaemonConfig(), web_chat_runtime_manager=runtime_manager
            )
        )
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers")

        providers = {p["name"]: p for p in response.json()["providers"]}
        assert providers["gemini"]["available"] is False
        assert providers["gemini"]["startup_error"] == "gemini failed"


class TestProviderModelsRoute:
    """Tests for GET /api/providers/models."""

    def test_returns_all_providers_with_models(self, client: TestClient) -> None:
        """Endpoint returns claude, gemini, qwen, and codex with model lists."""
        response = client.get("/api/providers/models")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        providers = {p["provider"]: p for p in data["providers"]}
        assert set(providers.keys()) == {"claude", "gemini", "qwen", "codex"}

        # Claude should have opus, sonnet, haiku
        claude_values = [m["value"] for m in providers["claude"]["models"]]
        assert claude_values == ["opus", "sonnet", "haiku"]
        assert providers["claude"]["models"][0]["reasoning"] == {
            "supported_efforts": ["low", "medium", "high", "max"]
        }

        # Gemini should expose the hardcoded web-chat defaults
        gemini = providers["gemini"]["models"]
        assert [m["value"] for m in gemini] == [
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
        ]
        assert [m["label"] for m in gemini] == ["pro-3.1", "flash-3"]

        # Qwen intentionally owns its provider slot even before a static model catalog exists
        qwen = providers["qwen"]["models"]
        assert qwen == []

        # Codex should expose the hardcoded web-chat defaults, not a placeholder
        codex = providers["codex"]["models"]
        assert [m["value"] for m in codex] == [
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.3-codex-spark",
            "gpt-5.2",
        ]
        assert [m["label"] for m in codex] == [
            "codex-5.4",
            "mini-5.4",
            "codex-5.3",
            "spark-5.3",
            "gpt-5.2",
        ]
        assert codex[0]["reasoning"] == {"supported_efforts": ["low", "medium", "high", "xhigh"]}

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
            assert providers["qwen"]["available"] is False
            assert providers["codex"]["available"] is False

    def test_models_route_uses_runtime_health_for_backend_failures(self) -> None:
        app = FastAPI()
        runtime_manager = MagicMock()
        runtime_manager.health.side_effect = lambda provider: SimpleNamespace(
            available=False if provider == "codex" else True,
            startup_error="codex failed" if provider == "codex" else None,
        )
        server = SimpleNamespace(
            services=SimpleNamespace(
                config=DaemonConfig(), web_chat_runtime_manager=runtime_manager
            )
        )
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert providers["codex"]["available"] is False
        assert providers["codex"]["startup_error"] == "codex failed"

    def test_models_route_prefers_provider_model_catalog_when_available(self) -> None:
        app = FastAPI()
        provider_model_catalog = MagicMock()
        provider_model_catalog.get_provider_snapshot.side_effect = lambda provider: {
            "source": "live",
            "models": (
                [
                    {"value": "gpt-5.4", "label": "gpt-5.4"},
                ]
                if provider == "codex"
                else [{"value": f"{provider}-model", "label": f"{provider}-label"}]
            ),
        }
        server = SimpleNamespace(
            services=SimpleNamespace(
                config=DaemonConfig(),
                provider_model_catalog=provider_model_catalog,
            )
        )
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert providers["claude"]["models"][0]["value"] == "claude-model"
        assert providers["gemini"]["models"][0]["value"] == "gemini-model"
        assert providers["qwen"]["models"][0]["value"] == "qwen-model"
        assert providers["codex"]["models"][0]["value"] == "gpt-5.4"
        assert providers["codex"]["source"] == "live"

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

    def test_current_catalog_ignores_partial_configured_lists(self) -> None:
        """Configured model lists do not alter the web-chat picker contract."""
        app = FastAPI()
        config = DaemonConfig(
            llm_providers=LLMProvidersConfig(
                codex=LLMProviderConfig(models="gpt-5.4,gpt-5.3-codex"),
                gemini=LLMProviderConfig(models="gemini-3.1-pro-preview,gemini-3-flash-preview"),
            )
        )
        server = SimpleNamespace(services=SimpleNamespace(config=config))
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")
        providers = {p["provider"]: p for p in response.json()["providers"]}

        assert providers["gemini"]["source"] == "static"
        assert providers["codex"]["source"] == "static"
        assert [m["value"] for m in providers["gemini"]["models"]] == [
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
        ]
        assert [m["value"] for m in providers["codex"]["models"]] == [
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.3-codex-spark",
            "gpt-5.2",
        ]
        assert [m["label"] for m in providers["codex"]["models"]] == [
            "codex-5.4",
            "mini-5.4",
            "codex-5.3",
            "spark-5.3",
            "gpt-5.2",
        ]

    def test_current_catalog_keeps_cli_supported_gemini_preview_models(self) -> None:
        """The web-chat picker stays on the current Gemini CLI-supported preview IDs."""
        app = FastAPI()
        config = DaemonConfig(
            llm_providers=LLMProvidersConfig(
                gemini=LLMProviderConfig(
                    models="gemini-3-pro-preview,gemini-3-flash-preview",
                ),
            )
        )
        server = SimpleNamespace(services=SimpleNamespace(config=config))
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")
        providers = {p["provider"]: p for p in response.json()["providers"]}

        assert [m["value"] for m in providers["gemini"]["models"]] == [
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
        ]
        assert [m["label"] for m in providers["gemini"]["models"]] == [
            "pro-3.1",
            "flash-3",
        ]

    def test_ignores_legacy_codex_config_entries(self) -> None:
        """Legacy Codex config models do not leak into the web-chat picker."""
        app = FastAPI()
        config = DaemonConfig(
            llm_providers=LLMProvidersConfig(
                codex=LLMProviderConfig(models="gpt-5.2,gpt-5,gpt-5-mini,o3"),
            )
        )
        server = SimpleNamespace(services=SimpleNamespace(config=config))
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")
        providers = {p["provider"]: p for p in response.json()["providers"]}

        assert [m["value"] for m in providers["codex"]["models"]] == [
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.3-codex-spark",
            "gpt-5.2",
        ]

    def test_filters_codex_models_to_documented_web_chat_surface(self) -> None:
        app = FastAPI()
        provider_model_catalog = MagicMock()

        def snapshot(provider: str) -> dict[str, object]:
            if provider != "codex":
                return {"source": "live", "models": [{"value": f"{provider}-model"}]}
            return {
                "source": "live",
                "models": [
                    {"value": "gpt-5.4", "label": "gpt-5.4"},
                    {"value": "gpt-5.2", "label": "gpt-5.2"},
                    {"value": "gpt-5.1-codex-max", "label": "gpt-5.1-codex-max"},
                    {"value": "gpt-5.1-codex", "label": "gpt-5.1-codex", "hidden": True},
                ],
            }

        provider_model_catalog.get_provider_snapshot.side_effect = snapshot
        server = SimpleNamespace(
            services=SimpleNamespace(
                config=DaemonConfig(),
                provider_model_catalog=provider_model_catalog,
            )
        )
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert [m["value"] for m in providers["codex"]["models"]] == ["gpt-5.4", "gpt-5.2"]
