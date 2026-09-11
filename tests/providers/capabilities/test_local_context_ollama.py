"""Native Ollama context evidence, using HTTP fakes and no live models."""

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
from gobby.providers.capabilities.local_context_ollama import discover_ollama_context

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

MODEL = "qwen2.5-coder"
DIGEST = "sha256:artifact-a"
IDENTITY = LocalContextIdentity(
    machine_id="machine-a",
    endpoint_id="ollama-a",
    configuration_fingerprint="credential-aware-fingerprint",
    api_base="http://localhost:11434/v1",
    api_key="private-token",
)


def _show(
    canonical: object = 262144,
    *,
    architecture: object = "qwen2",
    digest: object = DIGEST,
    **overrides: Any,
) -> dict[str, Any]:
    model_info = {
        "general.architecture": architecture,
        "qwen2.context_length": canonical,
        "llama.context_length": 999999,
    }
    return {
        "digest": digest,
        "model_info": model_info,
        "modelfile": "PARAMETER num_ctx 1048576",
        **overrides,
    }


def _running(
    context_length: object = 32768,
    *,
    model: object = f"{MODEL}:latest",
    name: object = f"{MODEL}:latest",
    digest: object = DIGEST,
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "model": model,
        "name": name,
        "digest": digest,
        "context_length": context_length,
        **overrides,
    }


async def _discover(
    show: object,
    ps: object,
    *,
    model_id: str = MODEL,
    identity: LocalContextIdentity = IDENTITY,
) -> LocalContextObservation:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer private-token"
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 10.0
        )
        if request.url.path == "/api/show":
            assert request.method == "POST"
            assert json.loads(request.content) == {"model": model_id}
            return httpx.Response(200, json=show)
        assert request.method == "GET"
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json=ps)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        return await discover_ollama_context(client, identity, model_id)


async def test_ollama_context_provenance() -> None:
    show = _show()
    del show["digest"]
    result = await _discover(show, {"models": [_running()]})

    assert result.effective_limit == 32768
    assert result.runtime_limit == 32768
    assert result.canonical_limit == 262144
    assert result.digest == DIGEST
    assert result.machine_id == "machine-a"
    assert result.endpoint_id == "ollama-a"
    assert result.configuration_fingerprint == IDENTITY.configuration_fingerprint
    assert result.diagnostics == ()
    assert result.provenance["canonical_limit"] == ("/api/show.model_info.qwen2.context_length")
    assert result.provenance["show_source"] == "/api/show"
    assert result.provenance["runtime_source"] == "/api/ps"
    assert result.instances[0].provenance == {
        "model_id": "/api/ps.models[0].model",
        "native_model_id": f"{MODEL}:latest",
        "runtime_limit": "/api/ps.models[0].context_length",
        "canonical_limit": "/api/show.model_info.qwen2.context_length",
        "architecture": "/api/show.model_info.general.architecture",
        "runtime_digest": DIGEST,
        "runtime_digest_source": "/api/ps.models[0].digest",
        "digest": DIGEST,
        "digest_source": "/api/ps.models[0].digest",
    }
    assert LocalContextObservation.from_dict(result.to_dict()) == result


async def test_ollama_invalid_runtime() -> None:
    multiple = await _discover(_show(), {"models": [_running(65536), _running("32768")]})
    assert multiple.effective_limit == 32768
    assert [instance.runtime_limit for instance in multiple.instances] == [65536, 32768]

    missing = await _discover(_show(), {"models": [_running(), _running(None)]})
    assert missing.effective_limit is None
    assert Diagnostic.RUNTIME_UNKNOWN in missing.diagnostics

    unrelated_architecture = await _discover(
        _show(architecture="mistral"), {"models": [_running()]}
    )
    assert unrelated_architecture.canonical_limit is None
    assert unrelated_architecture.effective_limit is None
    assert Diagnostic.CANONICAL_UNKNOWN in unrelated_architecture.diagnostics

    changed_digest = await _discover(_show(), {"models": [_running(digest="sha256:artifact-b")]})
    assert changed_digest.effective_limit is None
    assert Diagnostic.IDENTITY_CONFLICT in changed_digest.diagnostics

    invalid = await _discover(_show(), {"models": [_running("32k")]})
    assert invalid.effective_limit is None
    assert Diagnostic.INVALID_METADATA in invalid.diagnostics


@pytest.mark.parametrize(
    ("requested", "running", "expected"),
    [
        (MODEL, MODEL, 32768),
        (MODEL, f"{MODEL}:latest", 32768),
        (f"{MODEL}:latest", MODEL, 32768),
        (f"{MODEL}:latest", f"{MODEL}:latest", 32768),
        (MODEL, f"{MODEL}:q4", None),
        (f"org/{MODEL}", MODEL, None),
        (MODEL, f"prefix-{MODEL}", None),
        (MODEL, f" {MODEL} ", None),
    ],
)
async def test_ollama_name_matching(requested: str, running: str, expected: int | None) -> None:
    result = await _discover(
        _show(),
        {"models": [_running(model=running, name=running)]},
        model_id=requested,
    )
    assert result.effective_limit == expected
    if expected is None:
        assert Diagnostic.MODEL_NOT_FOUND in result.diagnostics


async def test_ollama_name_and_digest_conflicts_are_unknown() -> None:
    names = await _discover(_show(), {"models": [_running(name="different:latest")]})
    assert names.effective_limit is None
    assert Diagnostic.IDENTITY_CONFLICT in names.diagnostics

    digests = await _discover(
        _show(digest=None),
        {
            "models": [
                _running(65536, digest="sha256:artifact-a"),
                _running(32768, digest="sha256:artifact-b"),
            ]
        },
    )
    assert digests.effective_limit is None
    assert Diagnostic.IDENTITY_CONFLICT in digests.diagnostics


@pytest.mark.parametrize(
    ("show", "ps", "diagnostic"),
    [
        ({}, {"models": [_running()]}, Diagnostic.INVALID_METADATA),
        ([], {"models": [_running()]}, Diagnostic.INVALID_METADATA),
        (_show(), {}, Diagnostic.INVALID_METADATA),
        (_show(), {"models": []}, Diagnostic.MODEL_NOT_FOUND),
        (_show(canonical=0), {"models": [_running()]}, Diagnostic.INVALID_METADATA),
        (_show(digest=12), {"models": [_running()]}, Diagnostic.INVALID_METADATA),
        (_show(), {"models": [_running(digest={})]}, Diagnostic.INVALID_METADATA),
    ],
)
async def test_ollama_malformed_or_missing_evidence(
    show: object, ps: object, diagnostic: Diagnostic
) -> None:
    result = await _discover(show, ps)
    assert result.effective_limit is None
    assert diagnostic in result.diagnostics


async def test_ollama_ignores_modelfile_and_unrelated_architecture_limits() -> None:
    show = _show(architecture="mistral")
    show["model_info"] = {
        "general.architecture": "mistral",
        "qwen2.context_length": 262144,
    }
    result = await _discover(show, {"models": [_running()]})
    assert result.canonical_limit is None
    assert result.effective_limit is None


@pytest.mark.parametrize("base", ["", "/v1/", "/api/v1", "/api", "/proxy/v1"])
async def test_ollama_native_paths_and_optional_auth(base: str) -> None:
    paths: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        assert "authorization" not in request.headers
        if request.url.path.endswith("/api/show"):
            return httpx.Response(200, json=_show())
        return httpx.Response(200, json={"models": [_running()]})

    identity = replace(IDENTITY, api_base=f"http://localhost:11434{base}", api_key=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await discover_ollama_context(client, identity, MODEL)

    prefix = "/proxy" if base == "/proxy/v1" else ""
    assert paths == [("POST", f"{prefix}/api/show"), ("GET", f"{prefix}/api/ps")]
    assert result.effective_limit == 32768


@pytest.mark.parametrize(
    ("failure_path", "failure"),
    [
        ("show", "timeout"),
        ("show", "connect"),
        ("show", "status"),
        ("show", "redirect"),
        ("show", "json"),
        ("ps", "timeout"),
        ("ps", "connect"),
        ("ps", "status"),
        ("ps", "redirect"),
        ("ps", "json"),
    ],
)
async def test_ollama_endpoint_failures_are_sanitized(failure_path: str, failure: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        selected = request.url.path.endswith(f"/{failure_path}")
        if selected and failure == "timeout":
            raise httpx.ReadTimeout("private-token secret-password", request=request)
        if selected and failure == "connect":
            raise httpx.ConnectError("private-token secret-password", request=request)
        if selected and failure == "redirect":
            return httpx.Response(302, headers={"location": "http://other/private-token"})
        if selected and failure == "status":
            return httpx.Response(503, text="private-token secret-password")
        if selected and failure == "json":
            return httpx.Response(200, text="private-token secret-password")
        if request.url.path.endswith("/api/show"):
            return httpx.Response(200, json=_show())
        return httpx.Response(200, json={"models": [_running()]})

    identity = replace(
        IDENTITY,
        api_base="http://user:secret-password@localhost:11434/v1?key=private-token#secret",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        result = await discover_ollama_context(client, identity, MODEL)

    assert result.effective_limit is None
    expected = Diagnostic.INVALID_METADATA if failure == "json" else Diagnostic.ENDPOINT_UNAVAILABLE
    assert expected in result.diagnostics
    assert result.provenance["api_base"] == "http://localhost:11434"
    assert "private-token" not in json.dumps(result.to_dict())
    assert "secret-password" not in json.dumps(result.to_dict())
    assert len(requests) == (1 if failure_path == "show" and failure != "json" else 2)


async def test_ollama_cancellation_propagates() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(asyncio.CancelledError):
            await discover_ollama_context(client, IDENTITY, MODEL)
