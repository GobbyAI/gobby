"""Tests for conservative generic loopback context discovery."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from gobby.providers.capabilities.local_context import (
    ContextDiagnostic as Diagnostic,
)
from gobby.providers.capabilities.local_context import (
    LocalContextIdentity,
    LocalContextObservation,
)
from gobby.providers.capabilities.local_context_generic import discover_generic_context

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

MODEL = "publisher/model"
IDENTITY = LocalContextIdentity(
    machine_id="machine-a",
    endpoint_id="generic-a",
    configuration_fingerprint="credential-aware-fingerprint",
    api_base="http://private-user:private-password@localhost:8000/v1/",
    api_key="private-token",
)


async def _discover(
    payload: object,
    *,
    identity: LocalContextIdentity = IDENTITY,
    expected_url: str = "http://localhost:8000/v1/models",
) -> LocalContextObservation:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == httpx.URL(expected_url)
        assert request.headers["authorization"] == "Bearer private-token"
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 10.0
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        return await discover_generic_context(client, identity, MODEL)


@pytest.mark.parametrize(
    ("api_base", "expected_url"),
    [
        (
            "http://private-user:private-password@localhost:8000/v1/",
            "http://localhost:8000/v1/models",
        ),
        (
            "http://127.42.7.9:8000/proxy/v1",
            "http://127.42.7.9:8000/proxy/v1/models",
        ),
        ("http://[::1]:8000", "http://[::1]:8000/v1/models"),
    ],
)
async def test_generic_serving_limits(api_base: str, expected_url: str) -> None:
    observation = await _discover(
        {
            "object": "list",
            "data": [
                {
                    "id": MODEL,
                    "context_length": 65536,
                    "context_window": "32768",
                    "max_model_len": 49152,
                    "max_context_length": 1024,
                    "vendor_detail": {"limit": 1},
                },
                {"model": MODEL, "context_length": 24576},
                {"id": "other/model", "context_window": 1},
            ],
        },
        identity=replace(IDENTITY, api_base=api_base),
        expected_url=expected_url,
    )

    assert observation.provider == "openai-compatible"
    assert [instance.model_id for instance in observation.instances] == [MODEL, MODEL]
    assert observation.runtime_limit == 24576
    assert observation.effective_limit == 24576
    assert observation.diagnostics == ()
    assert "private" not in repr(observation.to_dict())
    assert observation.instances[0].provenance == {
        "model_id": "data[0].id",
        "runtime_limit": ("data[0].context_length,data[0].context_window,data[0].max_model_len"),
    }


async def test_generic_unknown_metadata() -> None:
    cases = [
        (
            {"data": [{"id": MODEL, "max_context_length": 262144}]},
            {Diagnostic.RUNTIME_UNKNOWN},
        ),
        (
            {"data": [{"id": MODEL, "context_length": 65536, "max_model_len": True}]},
            {Diagnostic.RUNTIME_UNKNOWN, Diagnostic.INVALID_METADATA},
        ),
        (
            {
                "data": [
                    {"id": MODEL, "context_window": 32768},
                    {"id": MODEL},
                ]
            },
            {Diagnostic.RUNTIME_UNKNOWN},
        ),
        (
            {
                "models": [
                    {"id": "other/model", "context_length": 1},
                    {"id": MODEL, "max_context_length": 262144},
                ]
            },
            {Diagnostic.RUNTIME_UNKNOWN},
        ),
        (
            {
                "data": [
                    {"context_length": 1},
                    {"id": MODEL, "context_length": 32768},
                ]
            },
            {Diagnostic.INVALID_METADATA},
        ),
        (
            {"data": [{"id": "other/model", "context_length": 1}]},
            {Diagnostic.MODEL_NOT_FOUND, Diagnostic.RUNTIME_UNKNOWN},
        ),
        (
            {"data": "malformed"},
            {
                Diagnostic.INVALID_METADATA,
                Diagnostic.MODEL_NOT_FOUND,
                Diagnostic.RUNTIME_UNKNOWN,
            },
        ),
    ]
    for payload, expected_diagnostics in cases:
        observation = await _discover(payload)
        assert observation.effective_limit is None
        assert expected_diagnostics <= set(observation.diagnostics)

    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"data": [{"id": MODEL, "context_length": 1}]})

    remote_hosts = [
        "https://example.com/v1",
        "http://localhost.example:8000/v1",
        "http://192.168.1.7:8000/v1",
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for api_base in remote_hosts:
            observation = await discover_generic_context(
                client,
                replace(IDENTITY, api_base=api_base),
                MODEL,
            )
            assert observation.effective_limit is None
            assert Diagnostic.NOT_LOCAL in observation.diagnostics
    assert called is False


@pytest.mark.parametrize("failure", ["http", "json", "request"])
async def test_generic_endpoint_failures_are_sanitized(failure: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "http":
            return httpx.Response(503, text="private upstream detail")
        if failure == "json":
            return httpx.Response(200, content=b"private invalid json")
        raise httpx.ConnectError("private connection detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        observation = await discover_generic_context(client, IDENTITY, MODEL)

    expected = Diagnostic.INVALID_METADATA if failure == "json" else Diagnostic.ENDPOINT_UNAVAILABLE
    assert expected in observation.diagnostics
    assert observation.effective_limit is None
    assert "private" not in repr(observation.to_dict())
    assert observation.provenance.get("http_status") == ("503" if failure == "http" else None)


async def test_generic_cancellation_propagates() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(asyncio.CancelledError):
            await discover_generic_context(client, IDENTITY, MODEL)
