"""Tests for provider availability API routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.config.ai import AIConfig, GenerationConfig, LocalGenerationConfig
from gobby.config.app import DaemonConfig
from gobby.servers.http import HTTPServer
from gobby.servers.local_provider_models import LocalEndpointModelGroup
from gobby.servers.provider_model_defaults import AGY_MODELS
from gobby.servers.routes.providers import create_providers_router

pytestmark = pytest.mark.unit


def _server_stub(**services: object) -> HTTPServer:
    services.setdefault("config", DaemonConfig())
    return cast(HTTPServer, SimpleNamespace(services=SimpleNamespace(**services)))


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
        assert names == ["claude", "codex", "droid", "grok", "qwen", "agy"]

    def test_list_providers_includes_configured_local_endpoints(self) -> None:
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    local=LocalGenerationConfig(
                        endpoints={
                            "studio": {
                                "provider": "lmstudio",
                                "api_base": "http://localhost:1234/v1",
                                "model": "local-model",
                            }
                        }
                    )
                )
            )
        )
        app = FastAPI()
        app.include_router(create_providers_router(_server_stub(config=config)))

        with patch("gobby.servers.routes.providers.shutil.which", return_value=None):
            response = TestClient(app).get("/api/providers")

        providers = {entry["name"]: entry for entry in response.json()["providers"]}
        assert providers["local:studio"]["available"] is True
        assert providers["local:studio"]["supports_web_chat"] is True

    def test_provider_available_when_binary_found(self, client: TestClient) -> None:
        """Provider is marked available when shutil.which finds the binary."""
        with patch("gobby.servers.routes.providers.shutil.which") as mock_which:
            mock_which.side_effect = lambda b: "/usr/bin/claude" if b == "claude" else None
            response = client.get("/api/providers")
            data = response.json()
            providers = {p["name"]: p for p in data["providers"]}
            assert set(providers) == {"claude", "codex", "droid", "grok", "qwen", "agy"}
            assert providers["claude"]["available"] is True
            assert providers["claude"]["path"] == "/usr/bin/claude"
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
            "grok": "/usr/local/bin/grok",
            "qwen": "/usr/local/bin/qwen",
            "codex": "/usr/local/bin/codex",
            "droid": "/usr/local/bin/droid",
            "agy": "/usr/local/bin/agy",
        }
        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: paths.get(b),
        ):
            response = client.get("/api/providers")
            data = response.json()
            for p in data["providers"]:
                assert p["available"] is (p["name"] != "agy")
                assert p["path"] == paths[p["name"]]
            providers = {p["name"]: p for p in data["providers"]}
            assert set(providers) == set(paths)
            assert providers["grok"]["supports_agent_spawn"] is True
            assert providers["agy"]["unavailable_reason"]

    def test_runtime_health_does_not_disable_lazy_acp_provider(self) -> None:
        app = FastAPI()
        runtime_manager = MagicMock()
        runtime_manager.health.side_effect = lambda provider: SimpleNamespace(
            available=False if provider == "qwen" else True,
            startup_error="Timed out starting Qwen ACP backend after 15.0s"
            if provider == "qwen"
            else None,
        )
        server = _server_stub(web_chat_runtime_manager=runtime_manager)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers")

        providers = {p["name"]: p for p in response.json()["providers"]}
        assert providers["qwen"]["available"] is True
        assert (
            providers["qwen"]["startup_error"] == "Timed out starting Qwen ACP backend after 15.0s"
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
            "grok",
            "qwen",
            "codex",
            "droid",
            "agy",
        }
        # Claude should expose explicit shorthand choices.
        claude_values = [m["value"] for m in providers["claude"]["models"]]
        assert claude_values == ["fable", "opus", "sonnet", "haiku"]
        assert providers["claude"]["models"][0]["reasoning"] == {
            "supported_efforts": ["low", "medium", "high", "xhigh", "max"]
        }
        claude_by_id = {m["value"]: m for m in providers["claude"]["models"]}
        assert claude_by_id["fable"]["context_length"] is None
        assert claude_by_id["fable"]["context_length_source"] == "unknown"
        assert claude_by_id["sonnet"]["context_length"] is None

        # Qwen intentionally owns its provider slot even before a static model catalog exists
        qwen = providers["qwen"]["models"]
        assert qwen == []

        grok = providers["grok"]["models"]
        assert [m["value"] for m in grok] == ["grok-composer-2.5-fast", "grok-build"]
        assert grok[0]["context_length"] == 200_000
        assert grok[0]["is_default"] is True
        assert grok[0]["reasoning"]["supported_efforts"] == [
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]
        assert grok[1]["context_length"] == 512_000

        agy_models = providers["agy"]["models"]
        assert [model["value"] for model in agy_models] == list(AGY_MODELS)
        assert providers["agy"]["source"] == "static"
        assert providers["agy"]["supports_web_chat"] is False
        assert providers["agy"]["available"] is False
        agy_by_id = {model["value"]: model for model in agy_models}
        assert "gemini-3.5-flash-low" not in agy_by_id
        assert "effort_display" not in agy_by_id["gemini-3.5-flash"]
        assert agy_by_id["gemini-3.5-flash"]["reasoning"] == {
            "supported_efforts": ["low", "medium", "high"],
            "default_effort": "low",
        }
        assert agy_by_id["gemini-3.5-flash"]["context_lookup_key"] == "gemini-3.5-flash"
        assert agy_by_id["gemini-3.5-flash"]["context_length"] == 1_048_576
        assert agy_by_id["gemini-3.1-pro"]["reasoning"] == {
            "supported_efforts": ["low", "high"],
            "default_effort": "high",
        }
        assert "effort_display" not in agy_by_id["claude-opus-4-6"]
        assert agy_by_id["claude-opus-4-6"]["reasoning"] == {
            "supported_efforts": ["high"],
            "default_effort": "high",
        }
        assert agy_by_id["gpt-oss-120b"]["context_length"] == 131_072

        # Codex should expose the hardcoded web-chat defaults, not a placeholder
        codex = providers["codex"]["models"]
        assert [m["value"] for m in codex] == [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.3-codex-spark",
            "gpt-5.2",
        ]
        assert [m["label"] for m in codex] == [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "codex-5.4",
            "mini-5.4",
            "codex-5.3",
            "spark-5.3",
            "gpt-5.2",
        ]
        assert codex[0]["reasoning"] == {"supported_efforts": ["low", "medium", "high", "xhigh"]}
        assert [m["context_length"] for m in codex] == [None] * 8
        assert {m["context_length_source"] for m in codex} == {"unknown"}

        droid_values = [m["value"] for m in providers["droid"]["models"]]
        assert len(droid_values) == 26
        assert "claude-fable-5" in droid_values
        assert "claude-opus-4-7" in droid_values
        assert "gpt-5.4" in droid_values
        assert "gemini-3.5-flash" in droid_values
        assert "gemini-3-flash-preview" in droid_values
        assert "minimax-m2.7" in droid_values
        droid_by_id = {m["value"]: m for m in providers["droid"]["models"]}
        assert droid_by_id["claude-fable-5"]["label"] == "Claude Fable 5"
        assert droid_by_id["claude-fable-5"]["context_length"] == 1_000_000
        assert droid_by_id["claude-fable-5"]["reasoning"] == {
            "supported_efforts": ["off", "low", "medium", "high", "xhigh", "max"],
            "default_effort": "high",
        }
        assert droid_by_id["claude-opus-4-7"]["context_length"] == 1_000_000
        assert droid_by_id["claude-sonnet-4-6"]["context_length"] == 200_000
        assert droid_by_id["gpt-5.4"]["context_length"] == 200_000
        assert droid_by_id["gemini-3.5-flash"]["label"] == "Gemini 3.5 Flash"
        assert droid_by_id["gemini-3.5-flash"]["context_length"] == 1_048_576
        assert droid_by_id["gemini-3.5-flash"]["reasoning"] == {
            "supported_efforts": ["minimal", "low", "medium", "high"],
            "default_effort": "medium",
        }

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
            assert set(providers) == {"claude", "codex", "droid", "grok", "qwen", "agy"}
            assert providers["claude"]["available"] is True
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
        server = _server_stub(web_chat_runtime_manager=runtime_manager)
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

    def test_models_route_keeps_lazy_acp_models_available_after_warmup_failure(self) -> None:
        app = FastAPI()
        runtime_manager = MagicMock()
        runtime_manager.health.side_effect = lambda provider: SimpleNamespace(
            available=False if provider == "grok" else True,
            startup_error="Timed out starting Grok ACP backend after 15.0s"
            if provider == "grok"
            else None,
        )
        server = _server_stub(web_chat_runtime_manager=runtime_manager)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert providers["grok"]["available"] is True
        assert (
            providers["grok"]["startup_error"] == "Timed out starting Grok ACP backend after 15.0s"
        )
        assert providers["grok"]["models"]

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
        server = _server_stub(provider_model_catalog=provider_model_catalog)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert set(providers) == {"claude", "codex", "droid", "grok", "qwen", "agy"}
        assert providers["claude"]["models"][0]["value"] == "claude-model"
        assert providers["qwen"]["models"][0]["value"] == "qwen-model"
        assert providers["codex"]["models"][0]["value"] == "gpt-5.4"
        assert providers["codex"]["models"][0]["context_length"] is None
        assert providers["codex"]["models"][0]["context_length_source"] == "unknown"
        assert providers["droid"]["models"][0]["value"] == "droid-model"
        assert [model["value"] for model in providers["agy"]["models"]] == list(AGY_MODELS)
        assert providers["agy"]["source"] == "static"
        assert providers["codex"]["source"] == "live"

    def test_models_route_merges_live_droid_gemini_family_models_with_static_metadata(
        self,
    ) -> None:
        app = FastAPI()
        provider_model_catalog = MagicMock()
        provider_model_catalog.get_provider_snapshot.side_effect = lambda provider: {
            "source": "live",
            "models": [{"value": "gemini-3.5-flash"}] if provider == "droid" else [],
        }
        server = _server_stub(provider_model_catalog=provider_model_catalog)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        droid = providers["droid"]["models"]
        assert providers["droid"]["source"] == "live"
        assert droid[0]["value"] == "gemini-3.5-flash"
        assert droid[0]["label"] == "Gemini 3.5 Flash"
        assert droid[0]["context_length"] == 1_048_576
        assert droid[0]["reasoning"] == {
            "supported_efforts": ["minimal", "low", "medium", "high"],
            "default_effort": "medium",
        }

    def test_models_route_falls_back_to_static_droid_when_catalog_empty(self) -> None:
        app = FastAPI()
        provider_model_catalog = MagicMock()
        provider_model_catalog.get_provider_snapshot.side_effect = lambda _provider: {
            "source": "failed",
            "models": [],
        }
        server = _server_stub(provider_model_catalog=provider_model_catalog)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert providers["droid"]["source"] == "static"
        droid_values = [m["value"] for m in providers["droid"]["models"]]
        assert "gemini-3.5-flash" in droid_values
        assert "gemini-3.1-pro-preview" in droid_values
        assert "gemini-3-flash-preview" in droid_values

    def test_generic_local_generation_provider_is_disabled_for_web_chat(self) -> None:
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    local=LocalGenerationConfig(
                        endpoints={
                            "lm-studio": {
                                "api_base": "http://localhost:1234/v1",
                                "model": "qwen-coder-32b",
                            }
                        }
                    )
                )
            ),
        )
        server = _server_stub(config=config)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        async def fake_discover(_name: str, _endpoint: object) -> LocalEndpointModelGroup:
            return LocalEndpointModelGroup(
                endpoint_name="lm-studio",
                provider_type="openai-compatible",
                provider_label="OpenAI Compatible",
                source="config",
                models=[
                    {
                        "value": "local:lm-studio",
                        "label": "Default (qwen-coder-32b)",
                        "canonical_id": "qwen-coder-32b",
                        "is_default": True,
                    }
                ],
            )

        with patch(
            "gobby.servers.routes.providers.discover_local_endpoint_model_group",
            side_effect=fake_discover,
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        local = providers["local:lm-studio"]

        assert "local" not in providers
        assert local["available"] is False
        assert local["display_name"] == "OpenAI Compatible"
        assert local["source"] == "config"
        assert local["supports_web_chat"] is False
        assert local["unavailable_reason"] == (
            "Generic OpenAI-compatible endpoints are unavailable for web chat"
        )
        assert "execution_provider" not in local
        assert local["models"] == [
            {
                "value": "local:lm-studio",
                "label": "Default (qwen-coder-32b)",
                "canonical_id": "qwen-coder-32b",
                "is_default": True,
            }
        ]

    def test_eligible_local_provider_executes_through_codex_without_native_mirrors(
        self,
    ) -> None:
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    local=LocalGenerationConfig(
                        endpoints={
                            "ollama-cloud": {
                                "provider": "ollama",
                                "api_base": "http://localhost:11434",
                                "model": "llama3.2:latest",
                            }
                        }
                    )
                )
            ),
        )
        server = _server_stub(config=config)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        async def fake_discover(_name: str, _endpoint: object) -> LocalEndpointModelGroup:
            return LocalEndpointModelGroup(
                endpoint_name="ollama-cloud",
                provider_type="ollama",
                provider_label="Ollama",
                source="live",
                models=[
                    {
                        "value": "local:ollama-cloud",
                        "label": "Default (llama3.2:latest)",
                        "canonical_id": "llama3.2:latest",
                        "is_default": True,
                    },
                    {
                        "value": "local:ollama-cloud/ollama/qwen3-coder",
                        "label": "Qwen3 Coder",
                        "canonical_id": "ollama/qwen3-coder",
                        "context_length": 65536,
                        "context_length_source": "provider_reported",
                    },
                ],
            )

        with patch(
            "gobby.servers.routes.providers.discover_local_endpoint_model_group",
            side_effect=fake_discover,
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        local = providers["local:ollama-cloud"]
        codex_model_values = {m["value"] for m in providers["codex"]["models"]}

        assert local["available"] is True
        assert local["display_name"] == "Ollama"
        assert local["execution_provider"] == "codex"
        assert local["source"] == "live"
        assert local["supports_web_chat"] is True
        assert local["unavailable_reason"] is None
        assert local["models"][1]["value"] == "local:ollama-cloud/ollama/qwen3-coder"
        assert "local:ollama-cloud" not in codex_model_values
        assert "local:ollama-cloud/ollama/qwen3-coder" not in codex_model_values

    def test_local_provider_discovery_failures_and_empty_results_are_disabled(
        self,
    ) -> None:
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    local=LocalGenerationConfig(
                        endpoints={
                            "studio-error": {
                                "provider": "lmstudio",
                                "api_base": "http://localhost:1234/v1",
                                "model": "qwen-coder-32b",
                            },
                            "studio-empty": {
                                "provider": "lmstudio",
                                "api_base": "http://localhost:1235/v1",
                                "model": "qwen-coder-7b",
                            },
                        }
                    )
                )
            ),
        )
        server = _server_stub(config=config)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        async def fake_discover(name: str, _endpoint: object) -> LocalEndpointModelGroup:
            return LocalEndpointModelGroup(
                endpoint_name=name,
                provider_type="lmstudio",
                provider_label="LM Studio",
                source="live",
                models=[],
                error="connection refused" if name == "studio-error" else None,
            )

        with patch(
            "gobby.servers.routes.providers.discover_local_endpoint_model_group",
            side_effect=fake_discover,
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        failed = providers["local:studio-error"]
        empty = providers["local:studio-empty"]

        assert failed["available"] is False
        assert failed["supports_web_chat"] is False
        assert failed["execution_provider"] == "codex"
        assert failed["startup_error"] == "connection refused"
        assert failed["unavailable_reason"] == "connection refused"
        assert empty["available"] is False
        assert empty["supports_web_chat"] is False
        assert empty["execution_provider"] == "codex"
        assert empty["unavailable_reason"] == "No completion-capable models discovered"

    def test_duplicate_local_provider_types_append_endpoint_names(self) -> None:
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    local=LocalGenerationConfig(
                        endpoints={
                            "studio-east": {
                                "provider": "lmstudio",
                                "api_base": "http://localhost:1234/v1",
                                "model": "model-east",
                            },
                            "studio-west": {
                                "provider": "lmstudio",
                                "api_base": "http://localhost:1235/v1",
                                "model": "model-west",
                            },
                        }
                    )
                )
            ),
        )
        server = _server_stub(config=config)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        async def fake_discover(name: str, _endpoint: object) -> LocalEndpointModelGroup:
            return LocalEndpointModelGroup(
                endpoint_name=name,
                provider_type="lmstudio",
                provider_label="LM Studio",
                source="live",
                models=[
                    {
                        "value": f"local:{name}",
                        "label": f"Default ({name})",
                        "canonical_id": name,
                        "is_default": True,
                    }
                ],
            )

        with patch(
            "gobby.servers.routes.providers.discover_local_endpoint_model_group",
            side_effect=fake_discover,
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}

        assert providers["local:studio-east"]["display_name"] == "LM Studio (studio-east)"
        assert providers["local:studio-west"]["display_name"] == "LM Studio (studio-west)"

    def test_current_catalog_uses_static_catalog_without_provider_config(self) -> None:
        """Provider model lists come from the catalog without daemon provider config."""
        app = FastAPI()
        config = DaemonConfig()
        server = _server_stub(config=config)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")
        providers = {p["provider"]: p for p in response.json()["providers"]}

        assert set(providers) == {"claude", "codex", "droid", "grok", "qwen", "agy"}
        assert providers["droid"]["source"] == "static"
        assert providers["codex"]["source"] == "static"
        assert [m["value"] for m in providers["claude"]["models"]] == [
            "fable",
            "opus",
            "sonnet",
            "haiku",
        ]
        assert [m["value"] for m in providers["codex"]["models"]] == [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.3-codex-spark",
            "gpt-5.2",
        ]
        assert [m["label"] for m in providers["codex"]["models"]] == [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "codex-5.4",
            "mini-5.4",
            "codex-5.3",
            "spark-5.3",
            "gpt-5.2",
        ]

    def test_current_catalog_uses_static_codex_models_without_provider_config(self) -> None:
        """Codex model rows come from provider discovery/static catalog."""
        app = FastAPI()
        config = DaemonConfig()
        server = _server_stub(config=config)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")
        providers = {p["provider"]: p for p in response.json()["providers"]}

        assert providers["codex"]["models"][0]["value"] == "gpt-5.6-sol"

    def test_current_catalog_keeps_gemini_family_models_in_droid_catalog(self) -> None:
        """Gemini-family models remain as Droid catalog model-family data."""
        app = FastAPI()
        config = DaemonConfig()
        server = _server_stub(config=config)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")
        providers = {p["provider"]: p for p in response.json()["providers"]}

        droid_by_id = {m["value"]: m for m in providers["droid"]["models"]}
        assert [
            droid_by_id[value]["label"]
            for value in (
                "gemini-3.5-flash",
                "gemini-3.1-pro-preview",
                "gemini-3-flash-preview",
            )
        ] == ["Gemini 3.5 Flash", "Gemini 3.1 Pro", "Gemini 3 Flash"]
        assert droid_by_id["gemini-3.5-flash"]["context_length"] == 1_048_576
        assert droid_by_id["gemini-3.5-flash"]["reasoning"]["default_effort"] == "medium"

    def test_filters_hidden_codex_models_from_web_chat_surface(self) -> None:
        """Only the provider-reported hidden flag excludes models from web chat.

        Regression guard: real models (e.g. gpt-5.6-sol) must never be dropped via
        value-based blocklists; see task #17775.
        """
        app = FastAPI()
        provider_model_catalog = MagicMock()

        def snapshot(provider: str) -> dict[str, object]:
            if provider != "codex":
                return {"source": "live", "models": [{"value": f"{provider}-model"}]}
            return {
                "source": "live",
                "models": [
                    {"value": "gpt-5.6-sol", "label": "gpt-5.6-sol"},
                    {"value": "gpt-5.4", "label": "gpt-5.4"},
                    {"value": "gpt-5.2", "label": "gpt-5.2"},
                    {"value": "gpt-5.1-codex-max", "label": "gpt-5.1-codex-max"},
                    {"value": "gpt-5.1-codex", "label": "gpt-5.1-codex", "hidden": True},
                ],
            }

        provider_model_catalog.get_provider_snapshot.side_effect = snapshot
        server = _server_stub(provider_model_catalog=provider_model_catalog)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        with patch(
            "gobby.servers.routes.providers.shutil.which",
            side_effect=lambda b: f"/usr/local/bin/{b}",
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert [m["value"] for m in providers["codex"]["models"]] == [
            "gpt-5.6-sol",
            "gpt-5.4",
            "gpt-5.2",
            "gpt-5.1-codex-max",
        ]
