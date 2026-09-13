"""Native LM Studio evidence, using an HTTP boundary fake and no live models."""

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from gobby.providers.capabilities.local_context import (
    ContextDiagnostic as Diagnostic,
)
from gobby.providers.capabilities.local_context import (
    LocalContextIdentity,
    LocalContextObservation,
)
from gobby.providers.capabilities.local_context_lmstudio import discover_lmstudio_context

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

MODEL = "publisher/model-Q4_K_M"
IDENTITY = LocalContextIdentity(
    machine_id="machine-a",
    endpoint_id="studio-a",
    configuration_fingerprint="credential-aware-fingerprint",
    api_base="http://localhost:1234/v1",
    api_key="private-token",
)


def _model(*limits: object, **overrides: Any) -> dict[str, Any]:
    return {
        "type": "llm",
        "key": MODEL,
        "max_context_length": 262144,
        "digest": "sha256:artifact-a",
        "revision": "revision-a",
        "loaded_instances": [
            {"id": f"instance-{index}", "config": {"context_length": limit}}
            for index, limit in enumerate(limits)
        ],
        **overrides,
    }


async def _discover(
    payload: object,
    *,
    model_id: str = MODEL,
    instance_id: str | None = None,
    identity: LocalContextIdentity = IDENTITY,
) -> LocalContextObservation:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/models"
        assert request.headers["authorization"] == "Bearer private-token"
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 10.0
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        return await discover_lmstudio_context(client, identity, model_id, instance_id)


@pytest.mark.parametrize(
    ("limits", "selected", "expected"),
    [
        ((32768,), None, 32768),
        ((65536, 32768), None, 32768),
        ((65536, 32768), "instance-0", 65536),
        ((65536, 32768), "instance-1", 32768),
        ((65536, None), "instance-0", 65536),
        ((65536, 32768), "missing", None),
        ((), None, None),
        ((), "instance-0", None),
    ],
)
async def test_lmstudio_instances(
    limits: tuple[object, ...], selected: str | None, expected: int | None
) -> None:
    result = await _discover({"models": [_model(*limits)]}, instance_id=selected)
    assert result.effective_limit == expected
    assert result.runtime_limit == expected
    assert result.model_id == MODEL
    assert result.instance_id == selected
    assert result.endpoint_id == "studio-a"
    assert result.machine_id == "machine-a"
    assert result.configuration_fingerprint == IDENTITY.configuration_fingerprint
    assert result.canonical_limit == (
        None if selected == "missing" or not limits and selected else 262144
    )
    expected_ids: list[str | None] = (
        [f"instance-{i}" for i in range(len(limits))] if limits else [None]
    )
    assert [i.instance_id for i in result.instances] == expected_ids
    assert LocalContextObservation.from_dict(result.to_dict()) == result


async def test_lmstudio_field_provenance() -> None:
    result = await _discover({"models": [_model(32768)]})
    assert result.canonical_limit == 262144
    assert result.digest == "sha256:artifact-a"
    assert result.diagnostics == ()
    assert result.instances[0].provenance == {
        "canonical_limit": "/api/v1/models.models[0].max_context_length",
        "runtime_limit": "/api/v1/models.models[0].loaded_instances[0].config.context_length",
        "model_id": "/api/v1/models.models[0].key",
        "instance_id": "/api/v1/models.models[0].loaded_instances[0].id",
        "digest": "sha256:artifact-a",
        "digest_source": "/api/v1/models.models[0].digest",
        "revision": "revision-a",
        "revision_source": "/api/v1/models.models[0].revision",
    }


async def test_lmstudio_unloaded_provenance() -> None:
    result = await _discover({"models": [_model()]})
    assert result.effective_limit is None
    assert result.canonical_limit == 262144
    assert result.digest == "sha256:artifact-a"
    assert result.instances[0].provenance["revision"] == "revision-a"
    assert result.instances[0].provenance["model_id"] == "/api/v1/models.models[0].key"
    assert result.diagnostics == (Diagnostic.RUNTIME_UNKNOWN,)


@pytest.mark.parametrize(
    ("payload", "diagnostic"),
    [
        ({"models": []}, Diagnostic.MODEL_NOT_FOUND),
        ({"models": [_model(1024, type="embedding")]}, Diagnostic.MODEL_NOT_FOUND),
        ({"models": [_model(1024, key="publisher/model-Q8_0")]}, Diagnostic.MODEL_NOT_FOUND),
        ({"models": [_model(1024, key=MODEL.upper())]}, Diagnostic.MODEL_NOT_FOUND),
        ({"models": [_model(1024, type=None)]}, Diagnostic.INVALID_METADATA),
        ({"models": [_model(1024, max_context_length=True)]}, Diagnostic.INVALID_METADATA),
        ({"models": [_model(1024, max_context_length=None)]}, Diagnostic.CANONICAL_UNKNOWN),
        ({"models": [_model(True)]}, Diagnostic.INVALID_METADATA),
        ({"models": [_model(-1)]}, Diagnostic.INVALID_METADATA),
        ({"models": [_model(0)]}, Diagnostic.INVALID_METADATA),
        ({"models": [_model(1.5)]}, Diagnostic.INVALID_METADATA),
        ({"models": [_model("32k")]}, Diagnostic.INVALID_METADATA),
        ({"models": [_model(2147483648)]}, Diagnostic.INVALID_METADATA),
        ({"models": [_model(1024, None)]}, Diagnostic.RUNTIME_UNKNOWN),
        ({"models": [_model(loaded_instances=[None])]}, Diagnostic.INVALID_METADATA),
        ({"models": [_model(loaded_instances={})]}, Diagnostic.INVALID_METADATA),
        (
            {"models": [_model(loaded_instances=[{"id": "i", "config": []}])]},
            Diagnostic.INVALID_METADATA,
        ),
        (
            {"models": [_model(loaded_instances=[{"config": {"context_length": 1024}}])]},
            Diagnostic.INVALID_METADATA,
        ),
        ({"models": [_model(1024), _model(None)]}, Diagnostic.RUNTIME_UNKNOWN),
        ({"models": [_model(1024), _model()]}, Diagnostic.RUNTIME_UNKNOWN),
        ({"models": [_model(1024), _model(2048, digest="other")]}, Diagnostic.IDENTITY_CONFLICT),
        ({"models": [_model(1024), _model(2048, revision="other")]}, Diagnostic.IDENTITY_CONFLICT),
        ([], Diagnostic.INVALID_METADATA),
        ({"models": None}, Diagnostic.INVALID_METADATA),
    ],
)
async def test_lmstudio_unknown_metadata(payload: object, diagnostic: Diagnostic) -> None:
    result = await _discover(payload)
    assert result.effective_limit is None
    assert diagnostic in result.diagnostics
    assert result.endpoint_id == IDENTITY.endpoint_id
    assert LocalContextObservation.from_dict(result.to_dict()) == result


async def test_lmstudio_mixed_catalog_and_duplicates() -> None:
    result = await _discover(
        {"models": [None, _model(1, type="embedding"), _model(65536), _model("32768")]}
    )
    assert result.canonical_limit == 262144
    assert result.effective_limit == 32768
    assert [i.runtime_limit for i in result.instances] == [65536, 32768]
    assert result.instances[0].provenance["runtime_limit"].startswith("/api/v1/models.models[2]")
    assert result.instances[1].provenance["runtime_limit"].startswith("/api/v1/models.models[3]")


@pytest.mark.parametrize(
    ("models", "expected"),
    [
        ([_model(32768, max_context_length=16384)], 16384),
        ([_model(65536), _model(32768, max_context_length=131072)], 32768),
    ],
)
async def test_lmstudio_contradictory_limits(models: list[dict[str, Any]], expected: int) -> None:
    result = await _discover({"models": models})
    assert result.effective_limit == expected
    assert Diagnostic.LIMIT_CONFLICT in result.diagnostics
    assert len(result.instances) == len(models)


@pytest.mark.parametrize("field", ["selected_variant", "quantization", "digest", "revision"])
async def test_lmstudio_variant_ambiguity_and_selection(field: str) -> None:
    left, right = _model(65536), _model(32768)
    left[field] = {"name": "Q4"} if field == "quantization" else "artifact-Q4"
    right[field] = {"name": "Q8"} if field == "quantization" else "artifact-Q8"
    right["loaded_instances"][0]["id"] = "instance-1"
    payload = {"models": [left, right]}
    ambiguous = await _discover(payload)
    selected = await _discover(payload, instance_id="instance-1")
    assert ambiguous.effective_limit is None
    assert Diagnostic.IDENTITY_CONFLICT in ambiguous.diagnostics
    assert selected.effective_limit == 32768
    assert len(selected.instances) == 2


@pytest.mark.parametrize("field", ["digest", "revision", "selected_variant"])
async def test_lmstudio_instance_artifact_conflict(field: str) -> None:
    model = _model(32768, **{field: "catalog-artifact"})
    model["loaded_instances"][0][field] = "different-instance-artifact"
    result = await _discover({"models": [model]})
    assert result.effective_limit is None
    assert Diagnostic.IDENTITY_CONFLICT in result.instances[0].diagnostics
    assert result.instances[0].provenance[field] == "different-instance-artifact"


@pytest.mark.parametrize("field", ["digest", "revision", "selected_variant"])
@pytest.mark.parametrize("value", [12, "", {}, []])
async def test_lmstudio_malformed_artifact(field: str, value: object) -> None:
    result = await _discover({"models": [_model(32768, **{field: value})]})
    assert result.effective_limit is None
    assert Diagnostic.INVALID_METADATA in result.instances[0].diagnostics


@pytest.mark.parametrize("value", [12, "Q8", [], {"name": 17}, {"name": ""}, {"name": []}])
@pytest.mark.parametrize("duplicate", [False, True])
async def test_lmstudio_malformed_quantization(value: object, duplicate: bool) -> None:
    models = [_model(8192, quantization=value)]
    if duplicate:
        models.insert(0, _model(65536, quantization={"name": "Q4"}))
    result = await _discover({"models": models})
    assert result.effective_limit is None
    assert len(result.instances) == len(models)
    assert Diagnostic.INVALID_METADATA in result.instances[-1].diagnostics


@pytest.mark.parametrize("value", [None, {"name": None}])
async def test_lmstudio_nullable_quantization(value: object) -> None:
    result = await _discover({"models": [_model(32768, quantization=value)]})
    assert result.effective_limit == 32768
    assert result.diagnostics == ()


async def test_lmstudio_observations_remain_independent() -> None:
    payload = {"models": [_model(None), _model(32768, key="another-model")]}
    unknown = await _discover(payload)
    known = await _discover(
        payload,
        model_id="another-model",
        identity=replace(IDENTITY, machine_id="machine-b", endpoint_id="studio-b"),
    )
    assert unknown.effective_limit is None
    assert known.effective_limit == 32768
    assert known.model_id == "another-model"
    assert known.machine_id == "machine-b"
    assert known.endpoint_id == "studio-b"
    assert known.diagnostics == ()


@pytest.mark.parametrize("failure", ["timeout", "connect", "status", "json", "redirect"])
async def test_lmstudio_endpoint_failures_are_sanitized(failure: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer private-token"
        if failure == "timeout":
            raise httpx.ReadTimeout("private-token secret-password", request=request)
        if failure == "connect":
            raise httpx.ConnectError("private-token secret-password", request=request)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "http://other-host/private-token"})
        return httpx.Response(
            503 if failure == "status" else 200, text="private-token secret-password"
        )

    identity = replace(
        IDENTITY, api_base="http://user:secret-password@localhost:1234/v1?key=private-token#secret"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        result = await discover_lmstudio_context(client, identity, MODEL)
    assert result.effective_limit is None
    assert result.endpoint_id == "studio-a"
    assert result.configuration_fingerprint == IDENTITY.configuration_fingerprint
    assert result.provenance["api_base"] == "http://localhost:1234"
    expected = Diagnostic.INVALID_METADATA if failure == "json" else Diagnostic.ENDPOINT_UNAVAILABLE
    assert expected in result.diagnostics
    assert len(requests) == 1
    assert "private-token" not in json.dumps(result.to_dict())
    assert "secret-password" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("base", ["", "/v1/", "/api/v1", "/proxy/v1"])
async def test_lmstudio_native_path_and_optional_auth(base: str) -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"models": [_model(32768)]})

    identity = replace(IDENTITY, api_base=f"http://localhost:1234{base}", api_key=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await discover_lmstudio_context(client, identity, MODEL)
    assert paths == ["/proxy/api/v1/models" if base == "/proxy/v1" else "/api/v1/models"]
    assert result.effective_limit == 32768


async def test_lmstudio_cancellation_propagates() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(asyncio.CancelledError):
            await discover_lmstudio_context(client, IDENTITY, MODEL)
