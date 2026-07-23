"""Tests for local OpenAI-compatible warmup in Qwen web chat."""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest

from gobby.config.ai import GenerationEndpointConfig
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

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
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


def _endpoint(
    *,
    protocol: str = "lmstudio",
    api_base: str = "http://localhost:1234/v1",
    model: str = "qwen3.6-35b-a3b-q8-local",
    api_key: str | None = "endpoint-token",
) -> GenerationEndpointConfig:
    return GenerationEndpointConfig(
        protocol=protocol,
        api_base=api_base,
        model=model,
        api_key=api_key,
    )


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
        local_generation_endpoints={"lm-studio": _endpoint()},
    )

    assert target == warmup.LocalOpenAIModelTarget(
        backend="lmstudio",
        request_model="qwen3.6-35b-a3b-q8-local",
        base_url="http://localhost:1234/v1",
        api_key="endpoint-token",
    )


def test_resolve_qwen_local_openai_target_uses_best_endpoint_match(
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
                        "id": "qwen3.6-35b-a3b-q8-local",
                        "baseUrl": "http://localhost:1234/v1",
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(warmup, "_QWEN_SETTINGS_PATH", settings_path)

    target = warmup.resolve_qwen_local_openai_target(
        "qwen3.6-35b-a3b-q8-local(openai)",
        project_path=None,
        local_generation_endpoints={
            "loose": _endpoint(model="qwen3.6-35b-a3b"),
            "exact": _endpoint(model="qwen3.6-35b-a3b-q8-local", api_key="exact-token"),
        },
    )

    assert target == warmup.LocalOpenAIModelTarget(
        backend="lmstudio",
        request_model="qwen3.6-35b-a3b-q8-local",
        base_url="http://localhost:1234/v1",
        api_key="exact-token",
    )


def test_resolve_qwen_local_openai_target_returns_none_for_ambiguous_endpoint(
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
                        "id": "qwen3.6-35b-a3b-q8-local",
                        "baseUrl": "http://localhost:1234/v1",
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(warmup, "_QWEN_SETTINGS_PATH", settings_path)

    target = warmup.resolve_qwen_local_openai_target(
        "qwen3.6-35b-a3b-q8-local(openai)",
        project_path=None,
        local_generation_endpoints={
            "exact-a": _endpoint(model="qwen3.6-35b-a3b-q8-local", api_key="a-token"),
            "exact-b": _endpoint(model="qwen3.6-35b-a3b-q8-local", api_key="b-token"),
        },
    )

    assert target is None


def test_resolve_qwen_local_openai_target_skips_openai_compatible_provider(
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
                        "id": "local-model",
                        "baseUrl": "http://localhost:8000/v1",
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(warmup, "_QWEN_SETTINGS_PATH", settings_path)

    target = warmup.resolve_qwen_local_openai_target(
        "local-model(openai)",
        project_path=None,
        local_generation_endpoints={
            "generic": _endpoint(
                protocol="openai-compatible",
                api_base="http://localhost:8000/v1",
                model="local-model",
            )
        },
    )

    assert target is None


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
        local_generation_endpoints={"lm-studio": _endpoint(api_key="real-lm-studio-token")},
    )

    assert fake_client.calls == [
        (
            "GET",
            "http://localhost:1234/api/v1/models",
            {
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer real-lm-studio-token",
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
                    "Authorization": "Bearer real-lm-studio-token",
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
                        "baseUrl": "http://localhost:1234/v1",
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(warmup, "_QWEN_SETTINGS_PATH", settings_path)

    fake_client = _FakeAsyncClient(
        {
            ("GET", "http://localhost:1234/api/ps"): [
                _FakeResponse(
                    "GET",
                    "http://localhost:1234/api/ps",
                    json_data={"models": []},
                )
            ],
            ("POST", "http://localhost:1234/api/chat"): [
                _FakeResponse("POST", "http://localhost:1234/api/chat", json_data={})
            ],
        }
    )
    monkeypatch.setattr(warmup.httpx, "AsyncClient", lambda: fake_client)

    await warmup.ensure_qwen_local_openai_model_ready(
        "qwen3-coder:32b(openai)",
        project_path=None,
        local_generation_endpoints={
            "ollama": _endpoint(
                protocol="ollama",
                api_base="http://localhost:1234/v1",
                model="qwen3-coder:32b",
                api_key=None,
            )
        },
    )

    assert fake_client.calls == [
        (
            "GET",
            "http://localhost:1234/api/ps",
            {"timeout": 10.0},
        ),
        (
            "POST",
            "http://localhost:1234/api/chat",
            {
                "json": {
                    "model": "qwen3-coder:32b",
                    "messages": [],
                    "keep_alive": -1,
                    "stream": False,
                },
                "timeout": 300.0,
            },
        ),
    ]


def _write_lm_studio_settings(settings_path: Path, env: dict[str, Any]) -> None:
    _write_qwen_settings(
        settings_path,
        {
            "security": {"auth": {"selectedType": "openai"}},
            "env": env,
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


def _resolve_lm_studio_target(
    settings_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    endpoint: GenerationEndpointConfig | None = None,
) -> warmup.LocalOpenAIModelTarget | None:
    monkeypatch.setattr(warmup, "_QWEN_SETTINGS_PATH", settings_path)
    endpoints = {"lm-studio": endpoint or _endpoint()}
    return warmup.resolve_qwen_local_openai_target(
        "qwen3.6-35b-a3b-q8-local(openai)",
        project_path=None,
        local_generation_endpoints=endpoints,
    )


def test_resolve_uses_endpoint_api_key_when_qwen_env_is_placeholder(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = temp_dir / ".qwen" / "settings.json"
    _write_lm_studio_settings(settings_path, {"LMSTUDIO_API_KEY": "lm-studio"})

    target = _resolve_lm_studio_target(settings_path, monkeypatch)

    assert target is not None
    assert target.api_key == "endpoint-token"


def test_resolve_ignores_qwen_env_token_in_favor_of_endpoint_config(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = temp_dir / ".qwen" / "settings.json"
    _write_lm_studio_settings(settings_path, {"LMSTUDIO_API_KEY": "user-set-token"})

    target = _resolve_lm_studio_target(settings_path, monkeypatch)

    assert target is not None
    assert target.api_key == "endpoint-token"


def test_resolve_returns_no_api_key_when_endpoint_has_no_api_key(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = temp_dir / ".qwen" / "settings.json"
    _write_lm_studio_settings(settings_path, {"LMSTUDIO_API_KEY": "user-set-token"})

    target = _resolve_lm_studio_target(
        settings_path,
        monkeypatch,
        endpoint=_endpoint(api_key=None),
    )

    assert target is not None
    assert target.api_key is None


@pytest.mark.asyncio
async def test_ensure_raises_actionable_error_on_lm_studio_401(
    temp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = temp_dir / ".qwen" / "settings.json"
    _write_lm_studio_settings(settings_path, {"LMSTUDIO_API_KEY": "lm-studio"})
    monkeypatch.setattr(warmup, "_QWEN_SETTINGS_PATH", settings_path)

    fake_client = _FakeAsyncClient(
        {
            ("GET", "http://localhost:1234/api/v1/models"): [
                _FakeResponse(
                    "GET",
                    "http://localhost:1234/api/v1/models",
                    status_code=401,
                    text="Unauthorized",
                )
            ],
        }
    )
    monkeypatch.setattr(warmup.httpx, "AsyncClient", lambda: fake_client)

    with pytest.raises(warmup.LocalOpenAIModelWarmupError) as exc_info:
        await warmup.ensure_qwen_local_openai_model_ready(
            "qwen3.6-35b-a3b-q8-local(openai)",
            project_path=None,
            local_generation_endpoints={"lm-studio": _endpoint(api_key="bad-token")},
        )

    message = str(exc_info.value)
    assert "401" in message
    assert "LM Studio API token" in message
    assert "ai.generation.endpoints" in message
    assert "disable API-key auth" in message
    # No model-load attempt once auth fails.
    assert all(call[0] != "POST" for call in fake_client.calls)


@pytest.mark.asyncio
async def test_ensure_resolves_qwen_settings_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    async def fake_to_thread(func: Any, *args: Any, **kwargs: Any) -> None:
        calls.append((func, args, kwargs))

    monkeypatch.setattr(warmup.asyncio, "to_thread", fake_to_thread)

    await warmup.ensure_qwen_local_openai_model_ready("qwen-local", project_path="/tmp/project")

    assert calls == [
        (
            warmup.resolve_qwen_local_openai_target,
            ("qwen-local",),
            {"project_path": "/tmp/project", "local_generation_endpoints": None},
        )
    ]
