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

    def test_list_providers_returns_all_supported_providers(self, client: TestClient) -> None:
        """Endpoint returns all supported provider entries."""
        response = client.get("/api/providers")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        names = [p["name"] for p in data["providers"]]
        assert names == ["claude", "codex", "droid", "gemini", "grok", "qwen", "agy"]

    def test_provider_available_when_binary_found(self, client: TestClient) -> None:
        """Provider is marked available when shutil.which finds the binary."""
        with patch("gobby.providers.registry.shutil.which") as mock_which:
            mock_which.side_effect = lambda b: "/usr/bin/claude" if b == "claude" else None
            response = client.get("/api/providers")
            data = response.json()
            providers = {p["name"]: p for p in data["providers"]}
            assert providers["claude"]["available"] is True
            assert providers["claude"]["path"] == "/usr/bin/claude"
            assert providers["gemini"]["available"] is False
            assert providers["gemini"]["path"] is None
            assert providers["grok"]["available"] is False
            assert providers["grok"]["path"] is None
            assert providers["qwen"]["available"] is False
            assert providers["qwen"]["path"] is None
            assert providers["codex"]["available"] is False
            assert providers["codex"]["path"] is None
            assert providers["droid"]["available"] is False
            assert providers["droid"]["path"] is None
            assert providers["agy"]["available"] is False
            assert providers["agy"]["supports_web_chat"] is False

    def test_all_providers_unavailable(self, client: TestClient) -> None:
        """All providers unavailable when no binaries found."""
        with patch("gobby.providers.registry.shutil.which", return_value=None):
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
            "grok": "/usr/local/bin/grok",
            "qwen": "/usr/local/bin/qwen",
            "codex": "/usr/local/bin/codex",
            "droid": "/usr/local/bin/droid",
            "agy": "/usr/local/bin/agy",
        }
        with patch(
            "gobby.providers.registry.shutil.which",
            side_effect=lambda b: paths.get(b),
        ):
            response = client.get("/api/providers")
            data = response.json()
            for p in data["providers"]:
                assert p["available"] is (p["name"] != "agy")
                assert p["path"] == paths[p["name"]]
            providers = {p["name"]: p for p in data["providers"]}
            assert providers["gemini"]["deprecated"] is True
            assert providers["grok"]["supports_agent_spawn"] is True
            assert providers["agy"]["unavailable_reason"]

    def test_runtime_health_does_not_disable_lazy_acp_provider(self) -> None:
        app = FastAPI()
        runtime_manager = MagicMock()
        runtime_manager.health.side_effect = lambda provider: SimpleNamespace(
            available=False if provider == "gemini" else True,
            startup_error="Timed out starting Gemini ACP backend after 15.0s"
            if provider == "gemini"
            else None,
        )
        server = SimpleNamespace(
            services=SimpleNamespace(
                config=DaemonConfig(), web_chat_runtime_manager=runtime_manager
            )
        )
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        with patch(
            "gobby.providers.registry.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers")

        providers = {p["name"]: p for p in response.json()["providers"]}
        assert providers["gemini"]["available"] is True
        assert providers["gemini"]["startup_error"] == (
            "Timed out starting Gemini ACP backend after 15.0s"
        )


class TestProviderModelsRoute:
    """Tests for GET /api/providers/models."""

    def test_returns_all_providers_with_models(self, client: TestClient) -> None:
        """Endpoint returns supported providers with model lists."""
        response = client.get("/api/providers/models")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        providers = {p["provider"]: p for p in data["providers"]}
        assert set(providers.keys()) == {
            "claude",
            "gemini",
            "grok",
            "qwen",
            "codex",
            "droid",
            "agy",
        }

        # Claude should have opus, sonnet, haiku
        claude_values = [m["value"] for m in providers["claude"]["models"]]
        assert claude_values == ["opus", "sonnet", "haiku"]
        assert providers["claude"]["models"][0]["reasoning"] == {
            "supported_efforts": ["low", "medium", "high", "xhigh", "max"]
        }
        assert providers["claude"]["models"][0]["context_length"] == 1_000_000

        # Gemini should expose the hardcoded web-chat defaults
        gemini = providers["gemini"]["models"]
        assert [m["value"] for m in gemini] == [
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
        ]
        assert [m["label"] for m in gemini] == ["pro-3.1", "flash-3"]
        assert [m["context_length"] for m in gemini] == [1_000_000, 1_000_000]
        assert gemini[0]["reasoning"] == {"supported_efforts": ["low", "medium", "high"]}

        # Qwen intentionally owns its provider slot even before a static model catalog exists
        qwen = providers["qwen"]["models"]
        assert qwen == []

        grok = providers["grok"]["models"]
        assert [m["value"] for m in grok] == ["grok-build"]
        assert grok[0]["context_length"] == 512_000

        assert providers["agy"]["models"] == []
        assert providers["agy"]["source"] == "unsupported"
        assert providers["agy"]["supports_web_chat"] is False
        assert providers["gemini"]["deprecated"] is True

        # Codex should expose the hardcoded web-chat defaults, not a placeholder
        codex = providers["codex"]["models"]
        assert [m["value"] for m in codex] == [
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.3-codex-spark",
            "gpt-5.2",
        ]
        assert [m["label"] for m in codex] == [
            "gpt-5.5",
            "codex-5.4",
            "mini-5.4",
            "codex-5.3",
            "spark-5.3",
            "gpt-5.2",
        ]
        assert codex[0]["reasoning"] == {"supported_efforts": ["low", "medium", "high", "xhigh"]}
        assert [m["context_length"] for m in codex] == [
            258_400,
            258_400,
            258_400,
            258_400,
            258_400,
            258_400,
        ]
        assert {m["context_length_source"] for m in codex} == {"static_default"}

        droid_values = [m["value"] for m in providers["droid"]["models"]]
        assert len(droid_values) == 24
        assert "claude-opus-4-7" in droid_values
        assert "gpt-5.4" in droid_values
        assert "gemini-3-flash-preview" in droid_values
        assert "minimax-m2.7" in droid_values
        droid_by_id = {m["value"]: m for m in providers["droid"]["models"]}
        assert droid_by_id["claude-opus-4-7"]["context_length"] == 1_000_000
        assert droid_by_id["gpt-5.4"]["context_length"] == 200_000

        # Each entry should have source field
        for p in data["providers"]:
            assert p["source"] == ("unsupported" if p["provider"] == "agy" else "static")

    def test_availability_reflects_binary_presence(self, client: TestClient) -> None:
        """Provider availability matches shutil.which results."""
        with patch(
            "gobby.providers.registry.shutil.which",
            side_effect=lambda b: "/bin/claude" if b == "claude" else None,
        ):
            response = client.get("/api/providers/models")
            providers = {p["provider"]: p for p in response.json()["providers"]}
            assert providers["claude"]["available"] is True
            assert providers["gemini"]["available"] is False
            assert providers["grok"]["available"] is False
            assert providers["qwen"]["available"] is False
            assert providers["codex"]["available"] is False
            assert providers["droid"]["available"] is False
            assert providers["agy"]["available"] is False

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
            "gobby.providers.registry.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert providers["codex"]["available"] is False
        assert providers["codex"]["startup_error"] == "codex failed"

    def test_models_route_keeps_lazy_acp_models_available_after_warmup_failure(self) -> None:
        app = FastAPI()
        runtime_manager = MagicMock()
        runtime_manager.health.side_effect = lambda provider: SimpleNamespace(
            available=False if provider == "gemini" else True,
            startup_error="Timed out starting Gemini ACP backend after 15.0s"
            if provider == "gemini"
            else None,
        )
        server = SimpleNamespace(
            services=SimpleNamespace(
                config=DaemonConfig(), web_chat_runtime_manager=runtime_manager
            )
        )
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        with patch(
            "gobby.providers.registry.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert providers["gemini"]["available"] is True
        assert providers["gemini"]["startup_error"] == (
            "Timed out starting Gemini ACP backend after 15.0s"
        )
        assert providers["gemini"]["models"]

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
            "gobby.providers.registry.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert providers["claude"]["models"][0]["value"] == "claude-model"
        assert providers["gemini"]["models"][0]["value"] == "gemini-model"
        assert providers["qwen"]["models"][0]["value"] == "qwen-model"
        assert providers["codex"]["models"][0]["value"] == "gpt-5.4"
        assert providers["codex"]["models"][0]["context_length"] == 258_400
        assert providers["codex"]["models"][0]["context_length_source"] == "static_default"
        assert providers["droid"]["models"][0]["value"] == "droid-model"
        assert providers["agy"]["models"] == []
        assert providers["agy"]["source"] == "unsupported"
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

    def test_current_catalog_prepends_partial_configured_claude_lists(self) -> None:
        """Claude binding model lists take precedence before static fallbacks."""
        app = FastAPI()
        config = DaemonConfig(
            llm_providers=LLMProvidersConfig(
                claude=LLMProviderConfig(models="haiku,sonnet"),
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
        assert [m["value"] for m in providers["claude"]["models"][:2]] == [
            "haiku",
            "sonnet",
        ]
        assert [m["value"] for m in providers["codex"]["models"]] == [
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.3-codex-spark",
            "gpt-5.2",
        ]
        assert [m["label"] for m in providers["codex"]["models"]] == [
            "gpt-5.5",
            "codex-5.4",
            "mini-5.4",
            "codex-5.3",
            "spark-5.3",
            "gpt-5.2",
        ]

    def test_current_catalog_uses_static_gemini_preview_models(self) -> None:
        """Gemini models come from current provider catalog, not llm_providers config."""
        app = FastAPI()
        config = DaemonConfig()
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

    def test_filters_hidden_codex_models_from_web_chat_surface(self) -> None:
        app = FastAPI()
        provider_model_catalog = MagicMock()

        def snapshot(provider: str) -> dict[str, object]:
            if provider != "codex":
                return {"source": "live", "models": [{"value": f"{provider}-model"}]}
            return {
                "source": "live",
                "models": [
                    {"value": "gpt-5.5", "label": "gpt-5.5"},
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
            "gobby.providers.registry.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert [m["value"] for m in providers["codex"]["models"]] == [
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.2",
            "gpt-5.1-codex-max",
        ]
