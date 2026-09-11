"""vLLM context evidence from the served model catalog."""

import asyncio
import json
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
from gobby.providers.capabilities.local_context_vllm import (
    discover_vllm_context,
    parse_vllm_served_records,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

MODEL = "publisher/model"
IDENTITY = LocalContextIdentity(
    machine_id="machine-a",
    endpoint_id="vllm-a",
    configuration_fingerprint="credential-aware-fingerprint",
    api_base="http://private-user:private-password@localhost:8000/v1/",
    api_key="private-token",
)


async def _discover(
    payload: object,
    *,
    model_id: str = MODEL,
    identity: LocalContextIdentity = IDENTITY,
) -> LocalContextObservation:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == httpx.URL("http://localhost:8000/v1/models")
        assert request.headers["authorization"] == "Bearer private-token"
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 10.0
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        return await discover_vllm_context(client, identity, model_id)


@pytest.mark.parametrize(
    ("limits", "expected", "diagnostics"),
    [
        ([32768], 32768, set()),
        (["65536", 32768], 32768, set()),
        ([65536, None], None, {Diagnostic.RUNTIME_UNKNOWN}),
        ([0], None, {Diagnostic.RUNTIME_UNKNOWN, Diagnostic.INVALID_METADATA}),
        ([True], None, {Diagnostic.RUNTIME_UNKNOWN, Diagnostic.INVALID_METADATA}),
        (["１２"], None, {Diagnostic.RUNTIME_UNKNOWN, Diagnostic.INVALID_METADATA}),
        ([2_147_483_648], None, {Diagnostic.RUNTIME_UNKNOWN, Diagnostic.INVALID_METADATA}),
    ],
)
async def test_vllm_serving_limits(
    limits: list[object],
    expected: int | None,
    diagnostics: set[Diagnostic],
) -> None:
    observation = await _discover(
        {
            "object": "list",
            "data": [
                {"id": MODEL, "object": "model", "max_model_len": limit}
                if limit is not None
                else {"id": MODEL, "object": "model"}
                for limit in limits
            ],
        }
    )

    assert len(observation.instances) == len(limits)
    assert observation.runtime_limit == expected
    assert observation.effective_limit == expected
    assert set(observation.diagnostics) == diagnostics
    assert "private" not in repr(observation.to_dict())
    for index, instance in enumerate(observation.instances):
        assert instance.provenance == {
            "model_id": f"data[{index}].id",
            "runtime_limit": f"data[{index}].max_model_len",
        }


async def test_vllm_preserves_duplicate_records_and_exact_identity() -> None:
    observation = await _discover(
        {
            "data": [
                {"id": MODEL, "max_model_len": 65536},
                {"id": "other/model", "max_model_len": 1},
                {"model": MODEL, "max_model_len": 32768},
            ]
        }
    )

    assert [instance.model_id for instance in observation.instances] == [MODEL, MODEL]
    assert observation.runtime_limit == 32768
    assert observation.effective_limit == 32768


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, {Diagnostic.INVALID_METADATA, Diagnostic.MODEL_NOT_FOUND}),
        ({"data": []}, {Diagnostic.MODEL_NOT_FOUND, Diagnostic.RUNTIME_UNKNOWN}),
        (
            {"data": [{"max_model_len": 32768}, {"id": MODEL, "max_model_len": 32768}]},
            {Diagnostic.INVALID_METADATA},
        ),
    ],
)
async def test_vllm_malformed_catalog_is_unknown(
    payload: object,
    expected: set[Diagnostic],
) -> None:
    observation = await _discover(payload)

    assert observation.effective_limit is None
    assert expected <= set(observation.diagnostics)


async def test_vllm_record_parser_preserves_records_and_projects_ids() -> None:
    catalog = parse_vllm_served_records(
        {
            "models": [
                {"id": "model-a", "max_model_len": 1},
                {"model": "model-a", "max_model_len": 2},
                {"name": " model-b ", "max_model_len": "3"},
                {"id": ""},
                "malformed",
            ]
        }
    )

    assert len(catalog.records) == 5
    assert catalog.model_ids() == ["model-a", "model-b"]
    assert [record.max_model_len for record in catalog.records] == [1, 2, 3, None, None]
    assert catalog.records[1].model_id_path == "models[1].model"
    assert catalog.records[2].max_model_len_path == "models[2].max_model_len"
    assert catalog.records[3].diagnostics == (Diagnostic.INVALID_METADATA,)
    assert catalog.records[4].diagnostics == (Diagnostic.INVALID_METADATA,)


@pytest.mark.parametrize("failure", ["http", "json", "request"])
async def test_vllm_endpoint_failures_are_sanitized(failure: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "http":
            return httpx.Response(503, text="private upstream detail")
        if failure == "json":
            return httpx.Response(200, content=b"private invalid json")
        raise httpx.ConnectError("private connection detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        observation = await discover_vllm_context(client, IDENTITY, MODEL)

    expected = Diagnostic.INVALID_METADATA if failure == "json" else Diagnostic.ENDPOINT_UNAVAILABLE
    assert expected in observation.diagnostics
    assert observation.effective_limit is None
    assert "private" not in repr(observation.to_dict())
    assert observation.provenance.get("http_status") == ("503" if failure == "http" else None)


async def test_vllm_native_path_and_optional_auth() -> None:
    identity = replace(
        IDENTITY,
        api_base="https://gateway.example/models/vllm",
        api_key=None,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/models/vllm/v1/models"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [{"id": MODEL, "max_model_len": 8192}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        observation = await discover_vllm_context(client, identity, MODEL)

    assert observation.effective_limit == 8192


async def test_vllm_cancellation_propagates() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(asyncio.CancelledError):
            await discover_vllm_context(client, IDENTITY, MODEL)


async def test_vllm_invalid_json_body() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps("not a catalog").encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        observation = await discover_vllm_context(client, IDENTITY, MODEL)

    assert set(observation.diagnostics) == {
        Diagnostic.INVALID_METADATA,
        Diagnostic.MODEL_NOT_FOUND,
        Diagnostic.RUNTIME_UNKNOWN,
    }
