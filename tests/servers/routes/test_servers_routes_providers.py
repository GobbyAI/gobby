"""Tests for provider availability API routes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.config.ai import AIConfig, GenerationConfig, GenerationEndpointConfig
from gobby.config.app import DaemonConfig
from gobby.providers.capabilities.models import (
    ActivationDescriptor,
    FactProvenance,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
    SpeedMode,
)
from gobby.providers.capabilities.resolve import CapabilityResolver
from gobby.providers.capabilities.seed import _agy_snapshot
from gobby.servers.http import HTTPServer
from gobby.servers.local_provider_models import LocalEndpointModelGroup
from gobby.servers.routes.providers import _configured_endpoints, create_providers_router

pytestmark = pytest.mark.unit


def _server_stub(*, config: object | None = None, **services: object) -> HTTPServer:
    # Mirror production shape: config lives on the server (HTTPServer.config
    # property); ServiceContainer has no ``config`` attribute.
    return cast(
        HTTPServer,
        SimpleNamespace(
            config=DaemonConfig() if config is None else config,
            services=SimpleNamespace(**services),
        ),
    )


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
    observed_at = datetime(2026, 8, 4, tzinfo=UTC)
    provenance = FactProvenance(
        source_key="test-source",
        source_url=None,
        observed_at=observed_at,
    )
    return ModelCapability(
        canonical_model=value,
        display_name=label or value,
        aliases=(),
        available=True,
        hidden=hidden,
        is_default=is_default,
        context_length=context_length,
        max_output_tokens=None,
        reasoning=(
            ReasoningSupport.KNOWN if supported_efforts is not None else ReasoningSupport.UNKNOWN
        ),
        supported_efforts=supported_efforts,
        default_effort=default_effort,
        latency_class=None,
        input_modalities=input_modalities,
        supports_tools=None,
        routes=(),
        provenance={"context_length": provenance} if context_length is not None else {},
    )


def _capability_service(**snapshots: tuple[ModelCapability, ...]) -> MagicMock:
    service = MagicMock()
    service.get_provider_snapshot.side_effect = lambda provider: (
        ProviderSnapshot(
            provider=provider,
            generation=1,
            models=snapshots[provider],
            sources=(
                SourceHealth(
                    source_key="test-source",
                    source_url=None,
                    required=True,
                    state=SourceState.OK,
                    attempts=1,
                    last_attempt_at=datetime(2026, 8, 4, tzinfo=UTC),
                    last_success_at=datetime(2026, 8, 4, tzinfo=UTC),
                    last_error=None,
                ),
            ),
        )
        if provider in snapshots
        else None
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
            assert providers["agy"]["supports_web_chat"] is True
            assert providers["agy"]["supports_agent_spawn"] is True

    def test_all_providers_unavailable(self, client: TestClient) -> None:
        """All providers unavailable when no binaries found."""
        with patch("gobby.servers.routes.providers.shutil.which", return_value=None):
            response = client.get("/api/providers")
            data = response.json()
            for p in data["providers"]:
                assert p["available"] is False
                assert p["path"] is None

    def test_all_supported_binaries_are_available(self, client: TestClient) -> None:
        """All binaries are available when AGY meets its version floor."""
        paths = {
            "claude": "/usr/local/bin/claude",
            "grok": "/usr/local/bin/grok",
            "qwen": "/usr/local/bin/qwen",
            "codex": "/usr/local/bin/codex",
            "droid": "/usr/local/bin/droid",
            "agy": "/usr/local/bin/agy",
        }
        support = SimpleNamespace(
            installed_version="1.1.18",
            required_version="1.1.18",
            supported=True,
            reason="AGY 1.1.18 meets required version 1.1.18.",
        )
        with (
            patch(
                "gobby.servers.routes.providers.shutil.which",
                side_effect=lambda binary: paths.get(binary),
            ),
            patch("gobby.providers.version_gate.peek_agy_support", return_value=support),
        ):
            providers = {p["name"]: p for p in client.get("/api/providers").json()["providers"]}

        assert set(providers) == set(paths)
        for name, provider in providers.items():
            assert provider["available"] is True
            assert provider["path"] == paths[name]
        assert providers["grok"]["supports_agent_spawn"] is True
        assert providers["agy"]["supports_web_chat"] is True
        assert providers["agy"]["supports_agent_spawn"] is True
        assert providers["agy"]["unavailable_reason"] is None

    def test_agy_below_version_floor_is_unavailable(self, client: TestClient) -> None:
        reason = "AGY 1.1.17 is installed; version 1.1.18 or newer is required."
        support = SimpleNamespace(
            installed_version="1.1.17",
            required_version="1.1.18",
            supported=False,
            reason=reason,
        )
        with (
            patch("gobby.servers.routes.providers.shutil.which", return_value="/usr/bin/agy"),
            patch("gobby.providers.version_gate.peek_agy_support", return_value=support),
        ):
            providers = {p["name"]: p for p in client.get("/api/providers").json()["providers"]}
            model_providers = {
                p["provider"]: p for p in client.get("/api/providers/models").json()["providers"]
            }

        for provider in (providers["agy"], model_providers["agy"]):
            assert provider["available"] is False
            assert provider["supports_web_chat"] is True
            assert provider["supports_agent_spawn"] is True
            assert provider["unavailable_reason"] == reason
        assert model_providers["agy"]["models"] == []
        assert model_providers["agy"]["refresh"]["sources"][0]["state"] == "pending"

    def test_agy_support_record_changes_are_visible_without_router_restart(
        self,
        client: TestClient,
    ) -> None:
        supported = SimpleNamespace(
            installed_version="1.1.18",
            required_version="1.1.18",
            supported=True,
            reason="AGY 1.1.18 meets required version 1.1.18.",
        )
        unsupported = SimpleNamespace(
            installed_version="1.1.17",
            required_version="1.1.18",
            supported=False,
            reason="AGY 1.1.17 is installed; version 1.1.18 or newer is required.",
        )
        current = {"record": supported}
        with (
            patch("gobby.servers.routes.providers.shutil.which", return_value="/usr/bin/agy"),
            patch(
                "gobby.providers.version_gate.peek_agy_support",
                side_effect=lambda: current["record"],
            ),
        ):
            first = {p["name"]: p for p in client.get("/api/providers").json()["providers"]}
            current["record"] = unsupported
            second = {p["name"]: p for p in client.get("/api/providers").json()["providers"]}
            models = {
                p["provider"]: p for p in client.get("/api/providers/models").json()["providers"]
            }

        assert first["agy"]["available"] is True
        assert first["agy"]["unavailable_reason"] is None
        assert second["agy"]["available"] is False
        assert second["agy"]["unavailable_reason"] == unsupported.reason
        assert models["agy"]["available"] is False
        assert models["agy"]["unavailable_reason"] == unsupported.reason
        assert models["agy"]["refresh"]["sources"][0]["state"] == "pending"

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


def _assert_models_response_matrix_shape() -> None:
    observed_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    provenance = FactProvenance(
        source_key="factory-docs",
        source_url="https://docs.factory.ai/models.md",
        observed_at=observed_at,
    )
    activation = ActivationDescriptor(
        kind="model_selector",
        surface="spawn-cli",
        params={},
    )
    model = ModelCapability(
        canonical_model="gpt-5.4",
        display_name="GPT-5.4",
        aliases=(),
        available=True,
        hidden=False,
        is_default=False,
        context_length=200_000,
        max_output_tokens=None,
        reasoning=ReasoningSupport.KNOWN,
        supported_efforts=("low", "medium", "high"),
        default_effort="medium",
        latency_class=None,
        input_modalities=("text",),
        supports_tools=True,
        routes=(
            ModelRoute(
                speed_mode=SpeedMode.STANDARD,
                selector="gpt-5.4",
                available=True,
                usage_multiplier=Decimal("1"),
                throughput_multiplier=None,
                latency_class=None,
                activations=(activation,),
                provenance={"usage_multiplier": provenance},
            ),
            ModelRoute(
                speed_mode=SpeedMode.FAST,
                selector="gpt-5.4-fast",
                available=True,
                usage_multiplier=Decimal("5"),
                throughput_multiplier=None,
                latency_class="fast",
                activations=(activation,),
                provenance={"usage_multiplier": provenance},
            ),
        ),
        provenance={"context_length": provenance},
    )
    snapshot = ProviderSnapshot(
        provider="droid",
        generation=12,
        models=(model,),
        sources=(
            SourceHealth(
                source_key="factory-docs",
                source_url="https://docs.factory.ai/models.md",
                required=True,
                state=SourceState.OK,
                attempts=1,
                last_attempt_at=observed_at,
                last_success_at=observed_at,
                last_error=None,
            ),
        ),
    )
    service = MagicMock()
    service.get_provider_snapshot.side_effect = lambda provider: (
        snapshot if provider == "droid" else None
    )
    app = FastAPI()
    app.include_router(create_providers_router(_server_stub(provider_capability_service=service)))

    response = TestClient(app).get("/api/providers/models")

    providers = {entry["provider"]: entry for entry in response.json()["providers"]}
    assert providers["droid"]["refresh"] == {
        "generation": 12,
        "sources": [
            {
                "source_key": "factory-docs",
                "source_url": "https://docs.factory.ai/models.md",
                "required": True,
                "state": "ok",
                "attempts": 1,
                "last_attempt_at": observed_at.isoformat(),
                "last_success_at": observed_at.isoformat(),
                "last_error": None,
            }
        ],
    }
    assert providers["droid"]["models"] == [
        {
            "canonical_model": "gpt-5.4",
            "display_name": "GPT-5.4",
            "aliases": [],
            "available": True,
            "hidden": False,
            "is_default": False,
            "context_length": {"value": 200_000, "source": "factory-docs"},
            "max_output_tokens": {"value": None, "source": "unknown"},
            "latency_class": None,
            "reasoning": {
                "status": "known",
                "supported_efforts": ["low", "medium", "high"],
                "default_effort": "medium",
            },
            "input_modalities": ["text"],
            "supports_tools": True,
            "routes": {
                "standard": {
                    "selector": "gpt-5.4",
                    "available": True,
                    "usage_multiplier": "1",
                    "throughput_multiplier": None,
                    "latency_class": None,
                    "activations": [
                        {"kind": "model_selector", "surface": "spawn-cli", "params": {}}
                    ],
                },
                "fast": {
                    "selector": "gpt-5.4-fast",
                    "available": True,
                    "usage_multiplier": "5",
                    "throughput_multiplier": None,
                    "latency_class": "fast",
                    "activations": [
                        {"kind": "model_selector", "surface": "spawn-cli", "params": {}}
                    ],
                },
            },
            "provenance": {"usage_multiplier": provenance.to_dict()},
        }
    ]


class TestProviderModelsRoute:
    """Tests for GET /api/providers/models."""

    def test_returns_all_providers_with_models(self, client: TestClient) -> None:
        """Endpoint returns supported providers with model lists."""
        with patch("gobby.servers.routes.providers.shutil.which", return_value=None):
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
        assert agy_models == []
        assert providers["agy"]["refresh"]["sources"][0]["state"] == "pending"
        assert providers["agy"]["supports_web_chat"] is True
        assert providers["agy"]["supports_agent_spawn"] is True
        assert providers["agy"]["available"] is False
        for provider in ("claude", "codex", "droid", "grok", "qwen", "agy"):
            assert providers[provider]["models"] == []
            assert providers[provider]["refresh"]["sources"][0]["state"] == "pending"

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

    def test_vllm_endpoint_with_failed_tool_probe_is_unavailable_for_web_chat(self) -> None:
        app = FastAPI()
        config = DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "vllm": {
                            "protocol": "vllm",
                            "api_base": "http://localhost:8321/v1",
                            "model": "auto",
                            "tool_chat": True,
                            "probed_model": "qwen-3b",
                            "probed_json": True,
                            "probed_tools": False,
                            "input_modalities": ["text"],
                        }
                    }
                )
            )
        )
        server = _server_stub(config=config)
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        async def fake_discover(
            name: str, endpoint: GenerationEndpointConfig
        ) -> LocalEndpointModelGroup:
            return LocalEndpointModelGroup(
                endpoint_name=name,
                provider_type="vllm",
                provider_label="vLLM",
                source="live",
                models=[
                    {
                        "value": "endpoint:vllm/qwen-3b",
                        "label": "Qwen 3B",
                        "canonical_id": "qwen-3b",
                    }
                ],
                probed_tools=endpoint.probed_tools,
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

        entry = {p["provider"]: p for p in response.json()["providers"]}["endpoint:vllm"]
        assert entry["available"] is False
        assert entry["supports_web_chat"] is False
        assert "--enable-auto-tool-choice" in entry["unavailable_reason"]
        assert "--tool-call-parser" in entry["unavailable_reason"]
        assert "execution_provider" not in entry

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
        assert providers["grok"]["refresh"]["sources"][0]["state"] == "pending"

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
        assert providers["claude"]["models"][0]["canonical_model"] == "claude-model"
        assert providers["qwen"]["models"][0]["canonical_model"] == "qwen-model"
        assert providers["codex"]["models"][0]["canonical_model"] == "gpt-5.4"
        assert providers["codex"]["models"][0]["context_length"] == {
            "value": None,
            "source": "unknown",
        }
        assert providers["droid"]["models"][0]["canonical_model"] == "droid-model"
        assert providers["agy"]["models"] == []
        assert providers["agy"]["refresh"]["sources"][0]["state"] == "pending"
        assert providers["codex"]["refresh"]["generation"] == 1

    def test_agy_models_route_uses_capability_snapshot_source_transitions(self) -> None:
        observed_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        bundled = _agy_snapshot(observed_at)
        live = replace(
            bundled,
            generation=7,
            sources=(
                SourceHealth(
                    source_key="agy_models_cli",
                    source_url=None,
                    required=True,
                    state=SourceState.OK,
                    attempts=1,
                    last_attempt_at=observed_at,
                    last_success_at=observed_at,
                    last_error=None,
                ),
            ),
        )
        current = {"snapshot": bundled}
        service = MagicMock()
        service.get_provider_snapshot.side_effect = lambda provider: (
            current["snapshot"] if provider == "agy" else None
        )
        support = SimpleNamespace(
            installed_version="1.1.18",
            required_version="1.1.18",
            supported=True,
            reason="AGY 1.1.18 meets required version 1.1.18.",
        )
        app = FastAPI()
        app.include_router(
            create_providers_router(_server_stub(provider_capability_service=service))
        )
        client = TestClient(app)

        with (
            patch("gobby.servers.routes.providers.shutil.which", return_value="/usr/bin/agy"),
            patch("gobby.providers.version_gate.peek_agy_support", return_value=support),
        ):
            first = {
                entry["provider"]: entry
                for entry in client.get("/api/providers/models").json()["providers"]
            }
            current["snapshot"] = live
            second = {
                entry["provider"]: entry
                for entry in client.get("/api/providers/models").json()["providers"]
            }

        assert first["agy"]["models"][0]["canonical_model"] == "gemini-3.7-flash-high"
        assert "value" not in first["agy"]["models"][0]
        assert first["agy"]["refresh"]["sources"][0]["source_key"] == "bundled"
        assert first["agy"]["refresh"]["sources"][0]["state"] == "stale"
        assert second["agy"]["refresh"]["generation"] == 7
        assert second["agy"]["refresh"]["sources"][0]["source_key"] == "agy_models_cli"
        assert second["agy"]["refresh"]["sources"][0]["state"] == "ok"

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
        assert providers["droid"]["refresh"]["generation"] == 1
        assert droid[0]["canonical_model"] == "gemini-3.5-flash"
        assert droid[0]["display_name"] == "Gemini 3.5 Flash"
        assert droid[0]["context_length"] == {
            "value": 1_048_576,
            "source": "test-source",
        }
        assert droid[0]["reasoning"] == {
            "status": "known",
            "supported_efforts": ["minimal", "low", "medium", "high"],
            "default_effort": "medium",
        }

    def test_models_route_resolves_registry_context_and_preserves_unknown(self) -> None:
        app = FastAPI()
        service = _capability_service(
            droid=(
                _model("registry-backed-model"),
                _model("unknown-model"),
            )
        )
        metadata_store = MagicMock()
        metadata_store.get_context_window.side_effect = {
            "registry-backed-model": 77_000,
        }.get
        resolver = CapabilityResolver(service, metadata_store)
        server = _server_stub(
            provider_capability_service=service,
            provider_capability_resolver=resolver,
        )
        app.include_router(create_providers_router(server))

        response = TestClient(app).get("/api/providers/models")

        providers = {entry["provider"]: entry for entry in response.json()["providers"]}
        models = {model["canonical_model"]: model for model in providers["droid"]["models"]}
        assert models["registry-backed-model"]["context_length"] == {
            "value": 77_000,
            "source": "registry",
        }
        assert models["unknown-model"]["context_length"] == {
            "value": None,
            "source": "unknown",
        }

    def test_models_route_has_no_static_droid_fallback(self) -> None:
        app = FastAPI()
        server = _server_stub(provider_capability_service=_capability_service())
        app.include_router(create_providers_router(server))
        client = TestClient(app)

        response = client.get("/api/providers/models")

        providers = {p["provider"]: p for p in response.json()["providers"]}
        assert providers["droid"]["refresh"]["sources"][0]["state"] == "pending"
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
        assert kimi["input_modalities"] is None

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
                "input_modalities": None,
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
            assert providers[provider]["refresh"]["sources"][0]["state"] == "pending"
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
        assert [m["canonical_model"] for m in providers["codex"]["models"]] == [
            "gpt-5.6-sol",
            "gpt-5.4",
            "gpt-5.2",
            "gpt-5.1-codex-max",
        ]


def test_models_response_matrix_shape() -> None:
    _assert_models_response_matrix_shape()


def test_cold_start_seed_and_pending() -> None:
    def seeded(provider: str) -> ProviderSnapshot:
        return ProviderSnapshot(
            provider=provider,
            generation=1,
            models=(_model(f"{provider}-seed"),),
            sources=(
                SourceHealth(
                    source_key="bundled",
                    source_url=None,
                    required=True,
                    state=SourceState.STALE,
                    attempts=0,
                    last_attempt_at=None,
                    last_success_at=None,
                    last_error=None,
                ),
            ),
        )

    snapshots = {provider: seeded(provider) for provider in ("claude", "droid")}
    service = MagicMock()
    service.get_provider_snapshot.side_effect = snapshots.get
    app = FastAPI()
    app.include_router(create_providers_router(_server_stub(provider_capability_service=service)))

    response = TestClient(app).get("/api/providers/models")

    providers = {entry["provider"]: entry for entry in response.json()["providers"]}
    for provider in ("claude", "droid"):
        assert providers[provider]["models"][0]["canonical_model"] == f"{provider}-seed"
        assert providers[provider]["refresh"]["sources"][0]["state"] == "stale"
    assert providers["qwen"]["models"] == []
    assert providers["qwen"]["refresh"] == {
        "generation": 0,
        "sources": [{"source_key": "local", "state": "pending"}],
    }


def test_agy_and_endpoint_groups_unchanged() -> None:
    config = DaemonConfig(
        ai=AIConfig(
            generation=GenerationConfig(
                endpoints={
                    "openrouter": {
                        "wire_api": "responses",
                        "api_base": "https://openrouter.ai/api/v1",
                        "model": "moonshotai/kimi-k3",
                    },
                    "studio": {
                        "protocol": "lmstudio",
                        "api_base": "http://localhost:1234/v1",
                        "model": "qwen-coder",
                    },
                }
            )
        )
    )
    app = FastAPI()
    app.include_router(create_providers_router(_server_stub(config=config)))

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
            side_effect=lambda binary: f"/usr/local/bin/{binary}",
        ),
    ):
        response = TestClient(app).get("/api/providers/models")

    providers = {entry["provider"]: entry for entry in response.json()["providers"]}
    assert providers["agy"]["models"] == []
    assert providers["agy"]["refresh"]["sources"][0]["state"] == "pending"
    assert any(
        model.get("value") == "endpoint:openrouter/moonshotai/kimi-k3"
        for model in providers["codex"]["models"]
    )
    assert providers["endpoint:studio"]["source"] == "live"
    assert providers["endpoint:studio"]["models"][0]["value"] == ("endpoint:studio/qwen-coder")
