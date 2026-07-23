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
