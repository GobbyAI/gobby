"""Acceptance coverage for the public revisioned configuration API."""

import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gobby.ai.endpoint_activation import EndpointActivationResult
from gobby.config.ai import GenerationEndpointConfig
from gobby.config.app import DaemonConfig
from gobby.config.registry import (
    CONFIG_REGISTRY,
    DYNAMIC_SEGMENT_CODEC_VECTORS,
    ConfigVisibility,
)
from gobby.config.runtime import ApplyFailure, ConfigSnapshot, RuntimeSecretBinding
from gobby.config.values import ConfigValuesService
from gobby.servers.routes.configuration_context import ConfigurationRouteContext
from gobby.servers.routes.configuration_generation_endpoints import (
    register_generation_endpoint_routes,
)
from gobby.servers.routes.configuration_values import register_value_routes
from gobby.servers.websocket.broadcast import BroadcastMixin
from gobby.storage.config_mutations import (
    ConfigConflictError,
    ConfigMutationResult,
    ConfigPatch,
    ConfigRevisionExhaustedError,
)

pytestmark = pytest.mark.unit


def _snapshot(
    revision: int,
    *,
    desired_config: DaemonConfig | None = None,
    desired_values: Mapping[str, object] | None = None,
    active_values: Mapping[str, object] | None = None,
    desired_secrets: Mapping[str, str] | None = None,
    active_secrets: Mapping[str, str] | None = None,
    pending_restart_keys: frozenset[str] = frozenset(),
    failed_live_keys: Mapping[str, ApplyFailure] | None = None,
) -> ConfigSnapshot:
    desired_bindings = {
        key: RuntimeSecretBinding(f"$secret:{key}", value, f"desired-{key}")
        for key, value in (desired_secrets or {}).items()
    }
    active_bindings = {
        key: RuntimeSecretBinding(f"$secret:{key}", value, f"active-{key}")
        for key, value in (active_secrets or {}).items()
    }
    desired = dict(desired_values or {})
    active = dict(active_values if active_values is not None else desired)
    return ConfigSnapshot(
        revision=revision,
        desired=desired_config or DaemonConfig(),
        active=desired_config or DaemonConfig(),
        row_revisions=dict.fromkeys(desired, revision),
        pending_restart_keys=pending_restart_keys,
        failed_live_keys=failed_live_keys or {},
        desired_values=desired,
        active_values=active,
        desired_bindings=desired_bindings,
        active_bindings=active_bindings,
    )


class _FakeRuntime:
    def __init__(self, snapshot: ConfigSnapshot) -> None:
        self.current = snapshot
        self.reconciled = snapshot
        self.reconcile_calls: list[int] = []

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self.current

    async def reconcile_local_commit(self, revision: int) -> ConfigSnapshot:
        self.reconcile_calls.append(revision)
        self.current = self.reconciled
        return self.current


class _FakeMutations:
    def __init__(
        self,
        result: ConfigMutationResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or ConfigMutationResult(0, frozenset())
        self.error = error
        self.calls: list[tuple[int, ConfigPatch]] = []

    def patch(self, *, expected_revision: int, patch: ConfigPatch) -> ConfigMutationResult:
        self.calls.append((expected_revision, patch))
        if self.error is not None:
            raise self.error
        return self.result


class _FakeContext:
    def __init__(self, service: object) -> None:
        self.service = service

    def get_config_service(self) -> Any:
        return self.service


def _service(
    snapshot: ConfigSnapshot,
    *,
    result: ConfigMutationResult | None = None,
    error: Exception | None = None,
) -> tuple[ConfigValuesService, _FakeRuntime, _FakeMutations]:
    runtime = _FakeRuntime(snapshot)
    mutations = _FakeMutations(result, error)
    service = ConfigValuesService(runtime=runtime, mutations=mutations)
    return service, runtime, mutations


def _client(service: object) -> TestClient:
    app = FastAPI()
    router = APIRouter(prefix="/api/config")
    context = cast(ConfigurationRouteContext, _FakeContext(service))
    register_value_routes(router, context)
    app.include_router(router)
    return TestClient(app)


def test_public_schema_and_values_contract() -> None:
    snapshot = _snapshot(
        7,
        desired_values={"websocket.ping_interval": 17.0, "auth.api_token_hash": "restricted"},
        active_values={"websocket.ping_interval": 13.0, "auth.api_token_hash": "restricted"},
        pending_restart_keys=frozenset({"websocket.enabled"}),
    )
    service, _runtime, _mutations = _service(snapshot)
    client = _client(service)

    schema_response = client.get("/api/config/schema")
    values_response = client.get("/api/config/values")

    assert schema_response.status_code == 200
    assert schema_response.json() == CONFIG_REGISTRY.json_schema(ConfigVisibility.PUBLIC)
    assert "auth.api_token_hash" not in schema_response.json()["properties"]
    assert values_response.status_code == 200
    assert values_response.json() == {
        "revision": 7,
        "desired": {"websocket": {"ping_interval": 17.0}},
        "active": {"websocket": {"ping_interval": 13.0}},
        "secret_set": {},
        "pending_restart_keys": ["websocket.enabled"],
        "failed_live_keys": {},
    }


def test_public_patch_contract() -> None:
    service, runtime, mutations = _service(
        _snapshot(4),
        result=ConfigMutationResult(
            5,
            frozenset({"websocket.ping_interval", "websocket.ping_timeout"}),
        ),
    )
    runtime.reconciled = _snapshot(5, desired_values={"websocket.ping_interval": 21.0})
    client = _client(service)

    response = client.patch(
        "/api/config/values",
        json={
            "expected_revision": 4,
            "values": {"websocket": {"ping_interval": 21.0}},
            "unset": ["websocket.ping_timeout"],
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 5
    expected_revision, patch = mutations.calls[-1]
    assert expected_revision == 4
    assert patch.values == {"websocket.ping_interval": 21.0}
    assert patch.unset == frozenset({"websocket.ping_timeout"})
    assert runtime.reconcile_calls == [5]

    stale_service, _runtime, _mutations = _service(
        _snapshot(5),
        error=ConfigConflictError(4, 5),
    )
    stale = _client(stale_service).patch(
        "/api/config/values",
        json={"expected_revision": 4, "values": {"websocket": {"ping_timeout": 4.0}}},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "revision_conflict"

    unknown = client.patch(
        "/api/config/values",
        json={"expected_revision": 5, "values": {"unknown": {"key": True}}},
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["path"] == ["values", "unknown", "key"]

    managed = client.patch(
        "/api/config/values",
        json={"expected_revision": 5, "values": {"ai": {"embeddings": {"model": "x"}}}},
    )
    assert managed.status_code == 422
    assert managed.json()["error"] == {
        "code": "managed_activation_required",
        "message": "Configuration key requires managed activation",
        "path": ["values", "ai", "embeddings", "model"],
        "retryable": False,
        "action": "/api/embeddings/switch/start",
    }

    secret = client.patch(
        "/api/config/values",
        json={
            "expected_revision": 5,
            "values": {
                "ai": {
                    "generation": {"endpoints": {"openrouter": {"api_key": "classified-secret"}}}
                }
            },
        },
    )
    assert secret.status_code == 200
    secret_patch = mutations.calls[-1][1]
    assert secret_patch.values == {}
    assert secret_patch.secrets["ai.generation.endpoints.openrouter.api_key"].plaintext == (
        "classified-secret"
    )


def test_public_surfaces_redact_secrets() -> None:
    secret_key = "databases.qdrant.api_key"
    plaintext = "never-return-this-secret"
    snapshot = _snapshot(
        8,
        desired_values={secret_key: "$secret:QDRANT_API_KEY"},
        active_values={secret_key: "$secret:QDRANT_API_KEY"},
        desired_secrets={secret_key: plaintext},
        active_secrets={secret_key: plaintext},
    )
    service, _runtime, _mutations = _service(snapshot)
    client = _client(service)

    values = client.get("/api/config/values")
    invalid = client.patch(
        "/api/config/values",
        json={"expected_revision": 8, "values": {"databases": {"qdrant": {"api_key": 3}}}},
    )

    assert values.status_code == 200
    assert values.json()["desired"]["databases"]["qdrant"]["api_key"] == "********"
    assert values.json()["secret_set"][secret_key] == {"desired": True, "active": True}
    assert invalid.status_code == 422
    assert plaintext not in values.text
    assert plaintext not in invalid.text


def test_legacy_reset_and_secrecy_flags_are_removed() -> None:
    service, _runtime, _mutations = _service(_snapshot(0))
    client = _client(service)

    assert client.post("/api/config/values/reset").status_code == 404
    assert client.put("/api/config/values", json={"values": {}}).status_code == 405
    response = client.patch(
        "/api/config/values",
        json={"expected_revision": 0, "values": {}, "is_secret": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "is_secret"]


class _RecordingBroadcaster(BroadcastMixin):
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def broadcast(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


@pytest.mark.asyncio
async def test_config_revision_event_contract() -> None:
    broadcaster = _RecordingBroadcaster()

    await broadcaster.broadcast_config_event(12)
    await broadcaster.broadcast_config_event(12)
    await broadcaster.broadcast_config_event(11)
    await broadcaster.broadcast_config_event(13)

    assert broadcaster.messages == [
        {"type": "config_event", "revision": 12},
        {"type": "config_event", "revision": 13},
    ]


def test_apply_failure_returns_committed_metadata() -> None:
    key = "websocket.ping_interval"
    failure = ApplyFailure(9, "websocket", frozenset({key}), "leaked-secret-value")
    service, runtime, _mutations = _service(
        _snapshot(8),
        result=ConfigMutationResult(9, frozenset({key})),
    )
    runtime.reconciled = _snapshot(
        9,
        desired_values={key: 22.0},
        active_values={key: 10.0},
        failed_live_keys={key: failure},
    )

    response = _client(service).patch(
        "/api/config/values",
        json={"expected_revision": 8, "values": {"websocket": {"ping_interval": 22.0}}},
    )

    assert response.status_code == 200
    assert response.json()["committed"] is True
    assert response.json()["revision"] == 9
    assert response.json()["apply_status"] == "failed_live"
    assert response.json()["failed_live_keys"][key] == {
        "revision": 9,
        "subscriber": "websocket",
    }
    assert "leaked-secret-value" not in response.text


class _EndpointService:
    def __init__(self, stored_secret: str | None = None) -> None:
        self.calls: list[tuple[int, dict[str, object]]] = []
        self.stored_secret = stored_secret

    def desired_config(self) -> DaemonConfig:
        return DaemonConfig()

    def desired_secret(self, _key: str) -> str | None:
        return self.stored_secret

    async def patch_flat(
        self,
        *,
        expected_revision: int,
        values: Mapping[str, object],
        unset: frozenset[str] = frozenset(),
        probe_verified: bool = False,
    ) -> dict[str, object]:
        assert not unset
        assert probe_verified is True
        self.calls.append((expected_revision, dict(values)))
        return {
            "committed": True,
            "revision": expected_revision + 1,
            "changed_keys": sorted(values),
            "apply_status": "applied",
            "pending_restart_keys": [],
            "failed_live_keys": {},
        }


def test_endpoint_activation_uses_typed_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _EndpointService()
    app = FastAPI()
    router = APIRouter(prefix="/api/config")
    register_generation_endpoint_routes(
        router,
        cast(ConfigurationRouteContext, _FakeContext(service)),
    )
    app.include_router(router)

    async def probe(
        _name: str,
        endpoint: GenerationEndpointConfig,
        _config: DaemonConfig,
    ) -> EndpointActivationResult:
        return EndpointActivationResult(endpoint=endpoint, vision_enabled=True)

    monkeypatch.setattr(
        "gobby.servers.routes.configuration_generation_endpoints.probe_responses_endpoint",
        probe,
    )
    response = TestClient(app).put(
        "/api/config/generation-endpoints/openrouter/activate",
        json={
            "expected_revision": 3,
            "api_base": "https://openrouter.example/v1",
            "api_key": "endpoint-secret",
            "model": "model-a",
        },
    )

    assert response.status_code == 200
    assert len(service.calls) == 1
    revision, values = service.calls[0]
    assert revision == 3
    assert values["ai.generation.endpoints.openrouter.api_key"] == "endpoint-secret"
    assert values["ai.generation.endpoints.openrouter.model"] == "model-a"


def test_endpoint_activation_omits_unchanged_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _EndpointService(stored_secret="endpoint-secret")
    app = FastAPI()
    router = APIRouter(prefix="/api/config")
    register_generation_endpoint_routes(
        router,
        cast(ConfigurationRouteContext, _FakeContext(service)),
    )
    app.include_router(router)

    async def probe(
        _name: str,
        endpoint: GenerationEndpointConfig,
        _config: DaemonConfig,
    ) -> EndpointActivationResult:
        return EndpointActivationResult(endpoint=endpoint, vision_enabled=True)

    monkeypatch.setattr(
        "gobby.servers.routes.configuration_generation_endpoints.probe_responses_endpoint",
        probe,
    )
    response = TestClient(app).put(
        "/api/config/generation-endpoints/openrouter/activate",
        json={
            "expected_revision": 3,
            "api_base": "https://openrouter.example/v1",
            "api_key": "endpoint-secret",
            "model": "model-a",
        },
    )

    assert response.status_code == 200
    _revision, values = service.calls[0]
    assert "ai.generation.endpoints.openrouter.api_key" not in values


def test_endpoint_activation_response_reports_probe_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _EndpointService()
    app = FastAPI()
    router = APIRouter(prefix="/api/config")
    register_generation_endpoint_routes(
        router,
        cast(ConfigurationRouteContext, _FakeContext(service)),
    )
    app.include_router(router)

    async def probe(
        _name: str,
        endpoint: GenerationEndpointConfig,
        _config: DaemonConfig,
    ) -> EndpointActivationResult:
        probed = endpoint.model_copy(
            update={
                "probed_model": endpoint.model,
                "input_modalities": ["text"],
                "probed_json": True,
                "probed_tools": False,
            }
        )
        return EndpointActivationResult(
            endpoint=probed,
            vision_enabled=False,
            diagnostics={"tools": "400: tools request rejected"},
        )

    monkeypatch.setattr(
        "gobby.servers.routes.configuration_generation_endpoints.probe_chat_completions_endpoint",
        probe,
    )
    response = TestClient(app).put(
        "/api/config/generation-endpoints/vllm/activate",
        json={
            "expected_revision": 3,
            "protocol": "vllm",
            "wire_api": "chat-completions",
            "api_base": "http://localhost:8321/v1",
            "model": "auto",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["probed_json"] is True
    assert body["probed_tools"] is False
    assert body["probe_diagnostics"] == {"tools": "400: tools request rejected"}
    _revision, values = service.calls[0]
    assert values["ai.generation.endpoints.vllm.probed_json"] is True
    assert values["ai.generation.endpoints.vllm.probed_tools"] is False
    assert not any("diagnostic" in key for key in values)


_VOICE_KEY = "voice.openai_compatible_audio"


def _voice_binding(api_key: str | None) -> dict[str, object]:
    return {
        "provider": "speaches",
        "url": "http://localhost:8080/v1",
        "model": "whisper-large-v3",
        "api_key": api_key,
    }


def _voice_patch_body(expected_revision: int, api_key: str | None) -> dict[str, object]:
    return {
        "expected_revision": expected_revision,
        "values": {"voice": {"openai_compatible_audio": [_voice_binding(api_key)]}},
    }


def test_voice_binding_api_key_rejects_plaintext() -> None:
    service, _runtime, mutations = _service(_snapshot(3))

    response = _client(service).patch(
        "/api/config/values",
        json=_voice_patch_body(3, "raw-plaintext-key"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "$secret:NAME" in response.json()["error"]["message"]
    assert "raw-plaintext-key" not in response.text
    assert mutations.calls == []


def test_voice_binding_accepts_reference_and_masks_reads() -> None:
    reference = "$secret:SPEACHES_KEY"
    stored = {_VOICE_KEY: [_voice_binding(reference)]}
    service, runtime, mutations = _service(
        _snapshot(5, desired_values=stored),
        result=ConfigMutationResult(6, frozenset({_VOICE_KEY})),
    )
    runtime.reconciled = _snapshot(6, desired_values=stored)
    client = _client(service)

    patched = client.patch("/api/config/values", json=_voice_patch_body(5, reference))
    values = client.get("/api/config/values")

    assert patched.status_code == 200
    submitted = cast(list[dict[str, object]], mutations.calls[-1][1].values[_VOICE_KEY])
    assert submitted[0]["api_key"] == reference
    binding = values.json()["desired"]["voice"]["openai_compatible_audio"][0]
    assert binding["api_key"] == "********"
    assert reference not in values.text


def test_voice_binding_masked_key_is_restored_from_anchored_epoch() -> None:
    reference = "$secret:SPEACHES_KEY"
    stored = {_VOICE_KEY: [_voice_binding(reference)]}
    service, runtime, mutations = _service(
        _snapshot(5, desired_values=stored),
        result=ConfigMutationResult(6, frozenset({_VOICE_KEY})),
    )
    runtime.reconciled = _snapshot(6, desired_values=stored)

    response = _client(service).patch(
        "/api/config/values",
        json=_voice_patch_body(5, "********"),
    )

    assert response.status_code == 200
    submitted = cast(list[dict[str, object]], mutations.calls[-1][1].values[_VOICE_KEY])
    assert submitted[0]["api_key"] == reference


def test_voice_binding_masked_keys_follow_provider_identity_when_reordered() -> None:
    first = {**_voice_binding("$secret:FIRST"), "provider": "first"}
    second = {**_voice_binding("$secret:SECOND"), "provider": "second"}
    stored = {_VOICE_KEY: [first, second]}
    service, runtime, mutations = _service(
        _snapshot(5, desired_values=stored),
        result=ConfigMutationResult(6, frozenset({_VOICE_KEY})),
    )
    runtime.reconciled = _snapshot(6, desired_values=stored)
    submitted = [
        {**second, "api_key": "********"},
        {**first, "api_key": "********"},
    ]

    response = _client(service).patch(
        "/api/config/values",
        json={
            "expected_revision": 5,
            "values": {"voice": {"openai_compatible_audio": submitted}},
        },
    )

    assert response.status_code == 200
    persisted = cast(list[dict[str, object]], mutations.calls[-1][1].values[_VOICE_KEY])
    assert [item["api_key"] for item in persisted] == ["$secret:SECOND", "$secret:FIRST"]


def test_voice_binding_contract_rejects_non_list_value() -> None:
    service, _runtime, mutations = _service(_snapshot(3))

    response = _client(service).patch(
        "/api/config/values",
        json={
            "expected_revision": 3,
            "values": {"voice": {"openai_compatible_audio": {}}},
        },
    )

    assert response.status_code == 422
    assert "must be a list" in response.json()["error"]["message"]
    assert mutations.calls == []


def test_internal_mutation_type_error_returns_logged_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _runtime, _mutations = _service(
        _snapshot(3),
        error=TypeError("adapter implementation failed"),
    )

    response = _client(service).patch(
        "/api/config/values",
        json={"expected_revision": 3, "values": {"ui": {"enabled": True}}},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "persistence_indeterminate"
    assert "Configuration persistence outcome is indeterminate" in caplog.text
    assert "adapter implementation failed" not in response.text


def test_voice_binding_masked_key_requires_matching_epoch() -> None:
    stored = {_VOICE_KEY: [_voice_binding("$secret:SPEACHES_KEY")]}
    service, _runtime, mutations = _service(_snapshot(6, desired_values=stored))

    response = _client(service).patch(
        "/api/config/values",
        json=_voice_patch_body(5, "********"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "revision_conflict"
    assert response.json()["error"]["actual_revision"] == 6
    assert mutations.calls == []


def test_patch_rejects_unprobed_responses_endpoint_creation() -> None:
    service, _runtime, mutations = _service(_snapshot(2))

    response = _client(service).patch(
        "/api/config/values",
        json={
            "expected_revision": 2,
            "values": {
                "ai": {
                    "generation": {
                        "endpoints": {
                            "neo": {
                                "wire_api": "responses",
                                "api_base": "https://neo.example/v1",
                                "model": "neo-model",
                            }
                        }
                    }
                }
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "probe_required"
    assert response.json()["error"]["action"] == "/api/config/generation-endpoints/neo/activate"
    assert mutations.calls == []


def test_probe_gate_is_anchored_to_expected_revision() -> None:
    service, _runtime, mutations = _service(_snapshot(2))

    response = _client(service).patch(
        "/api/config/values",
        json={
            "expected_revision": 1,
            "values": {
                "ai": {
                    "generation": {
                        "endpoints": {
                            "neo": {
                                "wire_api": "responses",
                                "api_base": "https://neo.example/v1",
                                "model": "neo-model",
                            }
                        }
                    }
                }
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "revision_conflict"
    assert mutations.calls == []


def test_patch_rejects_touching_existing_responses_endpoint() -> None:
    desired = DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "neo": {
                        "wire_api": "responses",
                        "api_base": "https://neo.example/v1",
                        "model": "neo-model",
                        "api_key": "$secret:NEO_KEY",
                    }
                }
            }
        }
    )
    snapshot = ConfigSnapshot(
        revision=3,
        desired=desired,
        active=desired,
        row_revisions={},
        pending_restart_keys=frozenset(),
        failed_live_keys={},
    )
    service, _runtime, mutations = _service(snapshot)

    response = _client(service).patch(
        "/api/config/values",
        json={
            "expected_revision": 3,
            "values": {"ai": {"generation": {"endpoints": {"neo": {"model": "other-model"}}}}},
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "probe_required"
    assert mutations.calls == []


def test_patch_rejects_unset_only_edit_of_existing_responses_endpoint() -> None:
    desired = DaemonConfig(
        ai={
            "generation": {
                "endpoints": {
                    "neo": {
                        "wire_api": "responses",
                        "api_base": "https://neo.example/v1",
                        "model": "neo-model",
                    }
                }
            }
        }
    )
    service, _runtime, mutations = _service(_snapshot(3, desired_config=desired))

    response = _client(service).patch(
        "/api/config/values",
        json={
            "expected_revision": 3,
            "values": {},
            "unset": ["ai.generation.endpoints.neo.model"],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "probe_required"
    assert mutations.calls == []


class _UnstartedRuntime:
    @property
    def snapshot(self) -> ConfigSnapshot:
        raise RuntimeError("ConfigRuntime has not started")

    async def reconcile_local_commit(self, revision: int) -> ConfigSnapshot:
        raise AssertionError("reconcile must not run before the runtime starts")


def test_values_returns_503_during_startup_window() -> None:
    service = ConfigValuesService(
        runtime=cast(Any, _UnstartedRuntime()),
        mutations=_FakeMutations(),
    )

    response = _client(service).get("/api/config/values")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"
    assert response.json()["error"]["retryable"] is True


@pytest.mark.parametrize(("logical", "encoded"), DYNAMIC_SEGMENT_CODEC_VECTORS)
def test_http_round_trips_codec_vectors(logical: str, encoded: str) -> None:
    key = f"ai.generation.endpoints.{encoded}.model"
    service, runtime, mutations = _service(
        _snapshot(1),
        result=ConfigMutationResult(2, frozenset({key})),
    )
    runtime.reconciled = _snapshot(2, desired_values={key: logical})
    client = _client(service)

    patch = client.patch(
        "/api/config/values",
        json={
            "expected_revision": 1,
            "values": {"ai": {"generation": {"endpoints": {encoded: {"model": logical}}}}},
        },
    )
    values = client.get("/api/config/values")

    assert patch.status_code == 200
    assert mutations.calls[-1][1].values == {key: logical}
    assert values.json()["desired"]["ai"]["generation"]["endpoints"][encoded]["model"] == logical
    assert (
        json.loads(values.text)["desired"]["ai"]["generation"]["endpoints"][encoded]["model"]
        == logical
    )


@pytest.mark.parametrize("revision", [-1, 1.5, "1", True, 1 << 53])
def test_revision_domain_and_exhaustion_contract(revision: object) -> None:
    service, _runtime, _mutations = _service(_snapshot(0))
    response = _client(service).patch(
        "/api/config/values",
        json={"expected_revision": revision, "values": {"websocket": {"ping_interval": 2.0}}},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "expected_revision"]

    exhausted_service, _runtime, _mutations = _service(
        _snapshot((1 << 53) - 1),
        error=ConfigRevisionExhaustedError(),
    )
    exhausted = _client(exhausted_service).patch(
        "/api/config/values",
        json={
            "expected_revision": (1 << 53) - 1,
            "values": {"websocket": {"ping_interval": 2.0}},
        },
    )
    assert exhausted.status_code == 422
    assert exhausted.json()["error"] == {
        "code": "revision_exhausted",
        "message": "Configuration revision cannot be advanced",
        "path": ["expected_revision"],
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_nested_patch_rejects_raw_dot_dynamic_segment() -> None:
    service, _runtime, mutations = _service(_snapshot(3))

    with pytest.raises(Exception) as error:
        await service.patch(
            expected_revision=3,
            values={"ai": {"generation": {"endpoints": {"foo.api_base": "https://x.example"}}}},
        )

    assert getattr(error.value, "code", None) == "validation_error"
    assert "canonically encoded" in getattr(error.value, "message", "")
    assert mutations.calls == []
