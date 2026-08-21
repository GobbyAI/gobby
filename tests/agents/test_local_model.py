"""Tests for local model preflight provider handling."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx
import pytest

from gobby.agents import local_model
from gobby.config.ai import GenerationEndpointConfig

pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(
        self,
        method: str,
        url: str,
        *,
        json_data: Any,
        status_code: int = 200,
    ) -> None:
        self.request = httpx.Request(method, url)
        self.status_code = status_code
        self._json_data = json_data
        self.text = ""

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code} error",
                request=self.request,
                response=self,
            )


class _FakeAsyncClient:
    def __init__(self, responses: dict[tuple[str, str], list[_FakeResponse]]) -> None:
        self._responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def _next_response(self, method: str, url: str) -> _FakeResponse:
        responses = self._responses.get((method, url))
        if not responses:
            raise AssertionError(f"Unexpected {method} {url}")
        return responses.pop(0)

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self._next_response("GET", url)

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self._next_response("POST", url)


@pytest.mark.asyncio
async def test_openai_compatible_preflight_does_not_manage_models() -> None:
    endpoint = GenerationEndpointConfig(
        api_base="http://localhost:8000/v1",
        model="local-model",
    )

    assert await local_model.ensure_local_model(endpoint) == "local-model"


@pytest.mark.asyncio
async def test_lmstudio_preflight_uses_native_model_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeAsyncClient(
        {
            ("GET", "http://localhost:1234/api/v1/models"): [
                _FakeResponse(
                    "GET",
                    "http://localhost:1234/api/v1/models",
                    json_data={
                        "models": [
                            {
                                "key": "google/gemma-4-26b-a4b",
                                "display_name": "Gemma",
                                "loaded_instances": [],
                            }
                        ]
                    },
                )
            ],
            ("POST", "http://localhost:1234/api/v1/models/load"): [
                _FakeResponse(
                    "POST",
                    "http://localhost:1234/api/v1/models/load",
                    json_data={"status": "loaded"},
                )
            ],
        }
    )
    monkeypatch.setattr(local_model.httpx, "AsyncClient", lambda: fake_client)
    endpoint = GenerationEndpointConfig(
        protocol="lmstudio",
        api_base="http://localhost:1234/v1",
        model="google/gemma-4-26b-a4b",
        api_key="token",
    )

    assert await local_model.ensure_local_model(endpoint) == "google/gemma-4-26b-a4b"
    assert fake_client.calls == [
        (
            "GET",
            "http://localhost:1234/api/v1/models",
            {
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer token",
                },
                "timeout": 15.0,
            },
        ),
        (
            "POST",
            "http://localhost:1234/api/v1/models/load",
            {
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer token",
                },
                "json": {"model": "google/gemma-4-26b-a4b"},
                "timeout": 300.0,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_ollama_preflight_swaps_models_with_keep_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeAsyncClient(
        {
            ("GET", "http://localhost:11434/api/ps"): [
                _FakeResponse(
                    "GET",
                    "http://localhost:11434/api/ps",
                    json_data={"models": [{"model": "old-model"}]},
                )
            ],
            ("POST", "http://localhost:11434/api/chat"): [
                _FakeResponse(
                    "POST",
                    "http://localhost:11434/api/chat",
                    json_data={"done": True},
                ),
                _FakeResponse(
                    "POST",
                    "http://localhost:11434/api/chat",
                    json_data={"done": True},
                ),
            ],
        }
    )
    monkeypatch.setattr(local_model.httpx, "AsyncClient", lambda: fake_client)
    endpoint = GenerationEndpointConfig(
        protocol="ollama",
        api_base="http://localhost:11434/v1",
        model="qwen3",
    )

    assert await local_model.ensure_local_model(endpoint) == "qwen3"
    assert fake_client.calls == [
        ("GET", "http://localhost:11434/api/ps", {"timeout": 10.0}),
        (
            "POST",
            "http://localhost:11434/api/chat",
            {
                "json": {
                    "model": "old-model",
                    "messages": [],
                    "keep_alive": 0,
                    "stream": False,
                },
                "timeout": 300.0,
            },
        ),
        (
            "POST",
            "http://localhost:11434/api/chat",
            {
                "json": {
                    "model": "qwen3",
                    "messages": [],
                    "keep_alive": -1,
                    "stream": False,
                },
                "timeout": 300.0,
            },
        ),
    ]


def _openai_models_response(url: str, model_ids: list[str]) -> _FakeResponse:
    return _FakeResponse(
        "GET",
        url,
        json_data={
            "object": "list",
            "data": [{"id": model_id, "object": "model"} for model_id in model_ids],
        },
    )


def _patch_httpx_client(monkeypatch: pytest.MonkeyPatch, fake_client: _FakeAsyncClient) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", lambda: fake_client)


def _assert_vllm_discovery_only(fake_client: _FakeAsyncClient, models_url: str) -> None:
    assert fake_client.calls, "expected GET /v1/models before any other vllm wire traffic"
    assert all(method == "GET" for method, _url, _kwargs in fake_client.calls)
    assert all(url == models_url for _method, url, _kwargs in fake_client.calls)
    for _method, url, kwargs in fake_client.calls:
        assert "/v1/v1/" not in url
        assert kwargs.get("json") is None
        assert "auto" not in url


@pytest.mark.asyncio
async def test_vllm_auto_resolves_before_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    models_url = "http://localhost:8000/v1/models"
    served = "Qwen/Qwen2.5-VL-7B-Instruct"
    fake_client = _FakeAsyncClient(
        {("GET", models_url): [_openai_models_response(models_url, [served])]}
    )
    _patch_httpx_client(monkeypatch, fake_client)
    endpoint = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://localhost:8000/v1",
        model="auto",
        api_key="token",
    )

    resolved = await local_model.ensure_local_model(endpoint)

    assert resolved == served
    assert resolved != "auto"
    assert fake_client.calls == [
        (
            "GET",
            models_url,
            {
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer token",
                },
                "timeout": 10.0,
            },
        )
    ]
    _assert_vllm_discovery_only(fake_client, models_url)

    shared_client = _FakeAsyncClient(
        {("GET", models_url): [_openai_models_response(models_url, [served])]}
    )
    _patch_httpx_client(monkeypatch, shared_client)
    assert await local_model.resolve_vllm_served_model(endpoint) == served
    _assert_vllm_discovery_only(shared_client, models_url)

    multi_client = _FakeAsyncClient(
        {("GET", models_url): [_openai_models_response(models_url, ["model-a", "model-b"])]}
    )
    _patch_httpx_client(monkeypatch, multi_client)
    with pytest.raises(local_model.LocalModelError, match="model-a") as multi_error:
        await local_model.ensure_local_model(endpoint)
    assert "model-b" in str(multi_error.value)
    _assert_vllm_discovery_only(multi_client, models_url)

    empty_client = _FakeAsyncClient(
        {("GET", models_url): [_openai_models_response(models_url, [])]}
    )
    _patch_httpx_client(monkeypatch, empty_client)
    with pytest.raises(local_model.LocalModelError, match="auto"):
        await local_model.ensure_local_model(endpoint)
    _assert_vllm_discovery_only(empty_client, models_url)

    explicit_client = _FakeAsyncClient(
        {("GET", models_url): [_openai_models_response(models_url, [served, "other-model"])]}
    )
    _patch_httpx_client(monkeypatch, explicit_client)
    explicit = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://localhost:8000/v1",
        model=served,
    )
    assert await local_model.ensure_local_model(explicit) == served
    _assert_vllm_discovery_only(explicit_client, models_url)

    missing_client = _FakeAsyncClient(
        {("GET", models_url): [_openai_models_response(models_url, ["other-model"])]}
    )
    _patch_httpx_client(monkeypatch, missing_client)
    missing = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://localhost:8000/v1",
        model=served,
    )
    with pytest.raises(local_model.LocalModelError, match=r"Qwen/Qwen2\.5-VL-7B-Instruct"):
        await local_model.ensure_local_model(missing)
    _assert_vllm_discovery_only(missing_client, models_url)


@pytest.mark.asyncio
async def test_vllm_models_url_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = (
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://localhost:8000/", "http://localhost:8000"),
        ("http://localhost:8000/v1", "http://localhost:8000"),
        ("http://localhost:8000/v1/", "http://localhost:8000"),
        ("https://gw.example/models/vllm/v1", "https://gw.example/models/vllm"),
        ("https://gw.example/models/vllm", "https://gw.example/models/vllm"),
    )
    for api_base, origin in cases:
        models_url = f"{origin}/v1/models"
        assert local_model.vllm_api_base(api_base) == f"{origin}/v1"
        assert local_model.vllm_models_url(api_base) == models_url
        assert local_model.vllm_health_url(api_base) == f"{origin}/health"
        fake_client = _FakeAsyncClient(
            {("GET", models_url): [_openai_models_response(models_url, ["only-model"])]}
        )
        _patch_httpx_client(monkeypatch, fake_client)
        endpoint = GenerationEndpointConfig(
            protocol="vllm",
            api_base=api_base,
            model="auto",
        )

        resolved = await local_model.resolve_vllm_served_model(endpoint)

        assert resolved == "only-model"
        _assert_vllm_discovery_only(fake_client, models_url)


class _RaisingAsyncClient(_FakeAsyncClient):
    def __init__(self, error: Exception) -> None:
        super().__init__({})
        self._error = error

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("GET", url, kwargs))
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        pytest.param(httpx.ReadTimeout("read timed out"), "Timed out", id="read-timeout"),
        pytest.param(httpx.ConnectTimeout("connect timed out"), "Timed out", id="connect-timeout"),
        pytest.param(httpx.ConnectError("refused"), "Cannot connect", id="connect-error"),
        pytest.param(httpx.RemoteProtocolError("closed"), "Cannot connect", id="protocol-error"),
    ],
)
async def test_vllm_resolver_maps_httpx_request_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    fragment: str,
) -> None:
    _patch_httpx_client(monkeypatch, _RaisingAsyncClient(error))
    endpoint = GenerationEndpointConfig(
        protocol="vllm",
        api_base="http://localhost:8000/v1",
        model="auto",
    )

    with pytest.raises(local_model.LocalModelError, match=fragment):
        await local_model.resolve_vllm_served_model(endpoint)
