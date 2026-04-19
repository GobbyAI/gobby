"""Tests for local OpenAI-compatible warmup in Qwen web chat."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from gobby.servers.websocket.chat import local_openai_warmup as warmup

pytestmark = pytest.mark.unit


class _FakeResponse:
    def __init__(
        self,
        method: str,
        url: str,
        *,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
    ) -> None:
        self.request = httpx.Request(method, url)
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

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

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def _next_response(self, method: str, url: str) -> _FakeResponse:
        key = (method, url)
        responses = self._responses.get(key)
        if not responses:
            raise AssertionError(f"Unexpected {method} {url}")
        return responses.pop(0)

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self._next_response("GET", url)

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self._next_response("POST", url)


def _write_qwen_settings(settings_path: Path, payload: dict[str, Any]) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(payload), encoding="utf-8")


def test_resolve_qwen_local_openai_target_reads_model_config(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = temp_dir / ".qwen" / "settings.json"
    _write_qwen_settings(
        settings_path,
        {
            "security": {"auth": {"selectedType": "openai"}},
            "env": {"LMSTUDIO_API_KEY": "lm-studio"},
            "modelProviders": {
                "openai": [
                    {
                        "id": "qwen3.6-35b-a3b-q8-local",
                        "baseUrl": "http://localhost:1234/v1",
                        "envKey": "LMSTUDIO_API_KEY",
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(warmup, "_QWEN_SETTINGS_PATH", settings_path)

    target = warmup.resolve_qwen_local_openai_target(
        "qwen3.6-35b-a3b-q8-local(openai)",
        project_path=None,
    )

    assert target == warmup.LocalOpenAIModelTarget(
        backend="lm_studio",
        request_model="qwen3.6-35b-a3b-q8-local",
        base_url="http://localhost:1234/v1",
        api_key="lm-studio",
    )


@pytest.mark.asyncio
async def test_ensure_qwen_local_openai_model_ready_loads_lm_studio_model(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = temp_dir / ".qwen" / "settings.json"
    _write_qwen_settings(
        settings_path,
        {
            "security": {"auth": {"selectedType": "openai"}},
            "env": {"LMSTUDIO_API_KEY": "lm-studio"},
            "modelProviders": {
                "openai": [
                    {
                        "id": "qwen3.6-35b-a3b-q8-local",
                        "baseUrl": "http://localhost:1234/v1",
                        "envKey": "LMSTUDIO_API_KEY",
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(warmup, "_QWEN_SETTINGS_PATH", settings_path)

    fake_client = _FakeAsyncClient(
        {
            ("GET", "http://localhost:1234/api/v1/models"): [
                _FakeResponse(
                    "GET",
                    "http://localhost:1234/api/v1/models",
                    json_data={
                        "models": [
                            {
                                "key": "qwen/qwen3.6-35b-a3b",
                                "display_name": "Qwen3.6 35B A3B",
                                "selected_variant": "qwen/qwen3.6-35b-a3b@q8_0",
                                "variants": ["qwen/qwen3.6-35b-a3b@q8_0"],
                                "loaded_instances": [],
                            }
                        ]
                    },
                )
            ],
            ("POST", "http://localhost:1234/api/v1/models/load"): [
                _FakeResponse("POST", "http://localhost:1234/api/v1/models/load", json_data={})
            ],
        }
    )
    monkeypatch.setattr(warmup.httpx, "AsyncClient", lambda: fake_client)

    await warmup.ensure_qwen_local_openai_model_ready(
        "qwen3.6-35b-a3b-q8-local(openai)",
        project_path=None,
    )

    assert fake_client.calls == [
        (
            "GET",
            "http://localhost:1234/api/v1/models",
            {
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer lm-studio",
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
                    "Authorization": "Bearer lm-studio",
                },
                "json": {"model": "qwen/qwen3.6-35b-a3b"},
                "timeout": 300.0,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_ensure_qwen_local_openai_model_ready_preloads_ollama_model(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = temp_dir / ".qwen" / "settings.json"
    _write_qwen_settings(
        settings_path,
        {
            "security": {"auth": {"selectedType": "openai"}},
            "modelProviders": {
                "openai": [
                    {
                        "id": "qwen3-coder:32b",
                        "baseUrl": "http://localhost:11434/v1",
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(warmup, "_QWEN_SETTINGS_PATH", settings_path)

    fake_client = _FakeAsyncClient(
        {
            ("GET", "http://localhost:11434/api/ps"): [
                _FakeResponse(
                    "GET",
                    "http://localhost:11434/api/ps",
                    json_data={"models": []},
                )
            ],
            ("POST", "http://localhost:11434/api/generate"): [
                _FakeResponse("POST", "http://localhost:11434/api/generate", json_data={})
            ],
        }
    )
    monkeypatch.setattr(warmup.httpx, "AsyncClient", lambda: fake_client)

    await warmup.ensure_qwen_local_openai_model_ready(
        "qwen3-coder:32b(openai)",
        project_path=None,
    )

    assert fake_client.calls == [
        (
            "GET",
            "http://localhost:11434/api/ps",
            {"timeout": 10.0},
        ),
        (
            "POST",
            "http://localhost:11434/api/generate",
            {
                "json": {
                    "model": "qwen3-coder:32b",
                    "keep_alive": -1,
                    "stream": False,
                },
                "timeout": 300.0,
            },
        ),
    ]
