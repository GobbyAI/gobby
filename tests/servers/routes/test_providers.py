"""Tests for provider availability API routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.config.ai import AIConfig, GenerationConfig
from gobby.config.app import DaemonConfig
from gobby.providers.capabilities.models import ModelCapability
from gobby.servers.http import HTTPServer
from gobby.servers.local_provider_models import LocalEndpointModelGroup
from gobby.servers.provider_model_defaults import AGY_MODELS
from gobby.servers.routes.providers import _configured_endpoints, create_providers_router

pytestmark = pytest.mark.unit


def _server_stub(**services: object) -> HTTPServer:
    services.setdefault("config", DaemonConfig())
    return cast(HTTPServer, SimpleNamespace(services=SimpleNamespace(**services)))


def _model(
    value: str,
    *,
    label: str | None = None,
    hidden: bool = False,
    is_default: bool = False,
    context_length: int | None = None,
    supported_efforts: tuple[str, ...] | None = None,
    default_effort: str | None = None,
    input_modalities: tuple[str, ...] | None = None,
) -> ModelCapability:
    return cast(
        ModelCapability,
        SimpleNamespace(
            canonical_model=value,
            display_name=label or value,
            hidden=hidden,
            is_default=is_default,
            context_length=context_length,
            supported_efforts=supported_efforts,
            default_effort=default_effort,
            input_modalities=input_modalities,
        ),
    )


def _capability_service(**snapshots: tuple[ModelCapability, ...]) -> MagicMock:
    service = MagicMock()
    service.get_provider_snapshot.side_effect = lambda provider: (
        SimpleNamespace(models=snapshots[provider]) if provider in snapshots else None
    )
    return service


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    router = create_providers_router()
    app.include_router(router)
    return TestClient(app)


def test_configured_endpoints_skips_unvalidated_values() -> None:
    matching = SimpleNamespace(wire_api="chat-completions")
    server = _server_stub(
        config=SimpleNamespace(
            ai=SimpleNamespace(
                generation=SimpleNamespace(
                    endpoints={
                        "raw-dict": {"wire_api": "chat-completions"},
                        "matching": matching,
                        "responses": SimpleNamespace(wire_api="responses"),
                    }
                )
            )
        )
    )

    assert list(_configured_endpoints(server, "chat-completions")) == [("matching", matching)]


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
                    endpoints={
                        "studio": {
                            "protocol": "lmstudio",
                            "api_base": "http://localhost:1234/v1",
                            "model": "local-model",
                        }
                    }
                )
            )
        )
        app = FastAPI()
        app.include_router(create_providers_router(_server_stub(config=config)))

        with patch("gobby.servers.routes.providers.shutil.which", return_value=None):
            response = TestClient(app).get("/api/providers")

        providers = {entry["name"]: entry for entry in response.json()["providers"]}
        assert providers["endpoint:studio"]["available"] is False
        assert providers["endpoint:studio"]["supports_web_chat"] is False

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
        for provider in ("claude", "codex", "droid", "grok", "qwen"):
            assert providers[provider]["models"] == []
            assert providers[provider]["source"] == "pending"

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
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "studio": {
                            "protocol": "lmstudio",
                            "api_base": "http://localhost:1234/v1",
                            "model": "qwen-coder",
                        }
                    }
                )
            )
        )
        runtime_manager = MagicMock()
        runtime_manager.health.side_effect = lambda provider: SimpleNamespace(
            available=False if provider == "codex" else True,
            startup_error="codex failed" if provider == "codex" else None,
        )
        server = _server_stub(config=config, web_chat_runtime_manager=runtime_manager)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        async def fake_discover(_name: str, _endpoint: object) -> LocalEndpointModelGroup:
            return LocalEndpointModelGroup(
                endpoint_name="studio",
                provider_type="lmstudio",
                provider_label="LM Studio",
                source="live",
                models=[
                    {
                        "value": "endpoint:studio/qwen-coder",
                        "label": "Qwen Coder",
                        "canonical_id": "qwen-coder",
                    }
                ],
            )

        with (
            patch(
                "gobby.servers.routes.providers.discover_local_endpoint_model_group",
                side_effect=fake_discover,
            ),
            patch(
                "gobby.servers.routes.providers.shutil.which",
                side_effect=lambda b: f"/usr/local/bin/{b}",
            ),
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert providers["codex"]["available"] is False
        assert providers["codex"]["startup_error"] == "codex failed"
        assert providers["endpoint:studio"]["available"] is False
        assert providers["endpoint:studio"]["unavailable_reason"] == "codex failed"

    def test_models_route_keeps_lazy_acp_provider_available_after_warmup_failure(self) -> None:
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
        assert providers["grok"]["models"] == []
        assert providers["grok"]["source"] == "pending"

    def test_models_route_uses_provider_capability_snapshots_when_available(self) -> None:
        app = FastAPI()
        service = _capability_service(
            claude=(_model("claude-model", label="claude-label"),),
            codex=(_model("gpt-5.4"),),
            droid=(_model("droid-model", label="droid-label"),),
            grok=(_model("grok-model", label="grok-label"),),
            qwen=(_model("qwen-model", label="qwen-label"),),
        )
        server = _server_stub(provider_capability_service=service)
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
        assert providers["codex"]["source"] == "provider_matrix"

    def test_models_route_serializes_droid_capability_snapshot(
        self,
    ) -> None:
        app = FastAPI()
        service = _capability_service(
            droid=(
                _model(
                    "gemini-3.5-flash",
                    label="Gemini 3.5 Flash",
                    context_length=1_048_576,
                    supported_efforts=("minimal", "low", "medium", "high"),
                    default_effort="medium",
                ),
            )
        )
        server = _server_stub(provider_capability_service=service)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        droid = providers["droid"]["models"]
        assert providers["droid"]["source"] == "provider_matrix"
        assert droid[0]["value"] == "gemini-3.5-flash"
        assert droid[0]["label"] == "Gemini 3.5 Flash"
        assert droid[0]["context_length"] == 1_048_576
        assert droid[0]["reasoning"] == {
            "supported_efforts": ["minimal", "low", "medium", "high"],
            "default_effort": "medium",
        }

    def test_models_route_has_no_static_droid_fallback(self) -> None:
        app = FastAPI()
        server = _server_stub(provider_capability_service=_capability_service())
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert providers["droid"]["source"] == "pending"
        assert providers["droid"]["models"] == []

    def test_responses_endpoint_is_grouped_under_codex_with_capabilities(self) -> None:
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "openrouter": {
                            "wire_api": "responses",
                            "api_base": "https://openrouter.ai/api/v1",
                            "api_key": "$secret:OPENROUTER_API_KEY",
                            "model": "moonshotai/kimi-k3",
                            "tool_chat": True,
                            "vision_extract": False,
                        }
                    }
                )
            ),
        )
        app.include_router(create_providers_router(_server_stub(config=config)))

        response = TestClient(app).get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert "endpoint:openrouter" not in providers
        kimi = next(
            model
            for model in providers["codex"]["models"]
            if model["value"] == "endpoint:openrouter/moonshotai/kimi-k3"
        )
        assert providers["codex"]["execution_provider"] == "codex"
        assert kimi["execution_provider"] == "codex"
        assert kimi["supports_tools"] is True
        assert kimi["input_modalities"] == ["text"]

    def test_generic_local_generation_provider_is_disabled_for_web_chat(self) -> None:
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "lm-studio": {
                            "api_base": "http://localhost:1234/v1",
                            "model": "qwen-coder-32b",
                        }
                    }
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
                        "value": "endpoint:lm-studio",
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
        local = providers["endpoint:lm-studio"]

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
                "value": "endpoint:lm-studio",
                "label": "Default (qwen-coder-32b)",
                "canonical_id": "qwen-coder-32b",
                "is_default": True,
            }
        ]

    def test_healthy_ollama_endpoint_is_available_for_web_chat(
        self,
    ) -> None:
        """LM Studio/Ollama endpoints route through Codex OSS in web chat (#19161)."""
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "ollama-cloud": {
                            "protocol": "ollama",
                            "api_base": "http://localhost:11434",
                            "model": "llama3.2:latest",
                        }
                    }
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
                        "value": "endpoint:ollama-cloud",
                        "label": "Default (llama3.2:latest)",
                        "canonical_id": "llama3.2:latest",
                        "is_default": True,
                    },
                    {
                        "value": "endpoint:ollama-cloud/ollama/qwen3-coder",
                        "label": "Qwen3 Coder",
                        "canonical_id": "ollama/qwen3-coder",
                        "context_length": 65536,
                        "context_length_source": "provider_reported",
                    },
                ],
            )

        with (
            patch(
                "gobby.servers.routes.providers.discover_local_endpoint_model_group",
                side_effect=fake_discover,
            ),
            patch(
                "gobby.servers.routes.providers.shutil.which",
                side_effect=lambda b: f"/usr/local/bin/{b}",
            ),
        ):
            response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        local = providers["endpoint:ollama-cloud"]
        codex_model_values = {m["value"] for m in providers["codex"]["models"]}

        assert local["available"] is True
        assert local["display_name"] == "Ollama"
        assert local["provider_type"] == "ollama"
        assert local["execution_provider"] == "codex"
        assert local["source"] == "live"
        assert local["supports_web_chat"] is True
        assert local["unavailable_reason"] is None
        assert local["models"][1]["value"] == "endpoint:ollama-cloud/ollama/qwen3-coder"
        assert "endpoint:ollama-cloud" not in codex_model_values
        assert "endpoint:ollama-cloud/ollama/qwen3-coder" not in codex_model_values

    def test_healthy_local_endpoint_without_codex_cli_stays_unavailable(
        self,
    ) -> None:
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "studio": {
                            "protocol": "lmstudio",
                            "api_base": "http://localhost:1234/v1",
                            "model": "qwen-coder-32b",
                        }
                    }
                )
            ),
        )
        server = _server_stub(config=config)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        async def fake_discover(_name: str, _endpoint: object) -> LocalEndpointModelGroup:
            return LocalEndpointModelGroup(
                endpoint_name="studio",
                provider_type="lmstudio",
                provider_label="LM Studio",
                source="live",
                models=[
                    {
                        "value": "endpoint:studio/qwen-coder-32b",
                        "label": "Qwen Coder 32B",
                        "canonical_id": "qwen-coder-32b",
                    }
                ],
            )

        with (
            patch(
                "gobby.servers.routes.providers.discover_local_endpoint_model_group",
                side_effect=fake_discover,
            ),
            patch("gobby.servers.routes.providers.shutil.which", return_value=None),
        ):
            response = client.get("/api/providers/models")

        local = {p["provider"]: p for p in response.json()["providers"]}["endpoint:studio"]

        assert local["available"] is False
        assert local["supports_web_chat"] is False
        assert "execution_provider" not in local
        assert local["unavailable_reason"] == (
            "Codex CLI is required to run local models in web chat"
        )

    def test_local_provider_discovery_failures_and_empty_results_are_disabled(
        self,
    ) -> None:
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "studio-error": {
                            "protocol": "lmstudio",
                            "api_base": "http://localhost:1234/v1",
                            "model": "qwen-coder-32b",
                        },
                        "studio-empty": {
                            "protocol": "lmstudio",
                            "api_base": "http://localhost:1235/v1",
                            "model": "qwen-coder-7b",
                        },
                    }
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
        failed = providers["endpoint:studio-error"]
        empty = providers["endpoint:studio-empty"]

        assert failed["available"] is False
        assert failed["supports_web_chat"] is False
        assert "execution_provider" not in failed
        assert failed["startup_error"] == "connection refused"
        assert failed["unavailable_reason"] == "connection refused"
        assert empty["available"] is False
        assert empty["supports_web_chat"] is False
        assert "execution_provider" not in empty
        assert empty["unavailable_reason"] == "No completion-capable models discovered"

    def test_duplicate_local_provider_types_append_endpoint_names(self) -> None:
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "studio-east": {
                            "protocol": "lmstudio",
                            "api_base": "http://localhost:1234/v1",
                            "model": "model-east",
                        },
                        "studio-west": {
                            "protocol": "lmstudio",
                            "api_base": "http://localhost:1235/v1",
                            "model": "model-west",
                        },
                    }
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
                        "value": f"endpoint:{name}",
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

        assert providers["endpoint:studio-east"]["display_name"] == "LM Studio (studio-east)"
        assert providers["endpoint:studio-west"]["display_name"] == "LM Studio (studio-west)"

    def test_missing_capability_service_returns_pending_empty_models(self) -> None:
        app = FastAPI()
        config = DaemonConfig()
        server = _server_stub(config=config)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")
        providers = {p["provider"]: p for p in response.json()["providers"]}

        assert set(providers) == {"claude", "codex", "droid", "grok", "qwen", "agy"}
        for provider in ("claude", "codex", "droid", "grok", "qwen"):
            assert providers[provider]["source"] == "pending"
            assert providers[provider]["models"] == []

    def test_filters_hidden_codex_models_from_web_chat_surface(self) -> None:
        """Only the provider-reported hidden flag excludes models from web chat.

        Regression guard: real models (e.g. gpt-5.6-sol) must never be dropped via
        value-based blocklists; see task #17775.
        """
        app = FastAPI()
        service = _capability_service(
            codex=(
                _model("gpt-5.6-sol"),
                _model("gpt-5.4"),
                _model("gpt-5.2"),
                _model("gpt-5.1-codex-max"),
                _model("gpt-5.1-codex", hidden=True),
            )
        )
        server = _server_stub(provider_capability_service=service)
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
