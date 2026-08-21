"""Wire-model resolution for daemon-built local ``tool_chat`` adapters."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.ai import AIAdapterStyle, AICapability, CapabilityBinding
from gobby.ai import _tool_chat_builder as tool_chat_builder
from gobby.ai._tool_chat_builder import _daemon_tool_chat_adapter_factories, _local_model_resolver
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolPolicy
from gobby.config.ai import AIConfig, GenerationConfig, GenerationEndpointConfig
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit


class _FakeMessage:
    content = "done"
    tool_calls: list[Any] = []


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices = [_FakeChoice()]
    usage = None


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse()


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self) -> None:
        self.chat = _FakeChat()


class _FakeLocalAdapter:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client


def _config() -> DaemonConfig:
    return DaemonConfig(
        ai=AIConfig(
            generation=GenerationConfig(
                endpoints={
                    "vllm-local": {
                        "protocol": "vllm",
                        "api_base": "http://localhost:8000/v1",
                        "model": "auto",
                        "tool_chat": True,
                    },
                    "generic": {
                        "protocol": "openai-compatible",
                        "api_base": "http://localhost:9000/v1",
                        "model": "gemma",
                        "tool_chat": True,
                    },
                }
            )
        ),
    )


def _binding(endpoint_name: str, protocol: str, model: str) -> CapabilityBinding:
    return CapabilityBinding(
        capability=AICapability.TOOL_CHAT,
        provider=f"endpoint:{endpoint_name}",
        adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
        available=True,
        models=(model,),
        metadata={"endpoint": endpoint_name, "protocol": protocol},
    )


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, served: str) -> list[str]:
    requested: list[str] = []

    async def fake_resolve(endpoint: GenerationEndpointConfig) -> str:
        requested.append(endpoint.model)
        return served

    monkeypatch.setattr(tool_chat_builder, "resolve_vllm_served_model", fake_resolve)
    return requested


@pytest.mark.asyncio
async def test_local_model_resolver_resolves_vllm_through_served_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = _patch_resolver(monkeypatch, "Qwen/Qwen2.5-VL-7B-Instruct")
    resolve = _local_model_resolver(_config())
    vllm = _binding("vllm-local", "vllm", "auto")
    generic = _binding("generic", "openai-compatible", "gemma")

    assert await resolve(vllm, None) == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert await resolve(vllm, "auto") == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert await resolve(vllm, "explicit-id") == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert requested == ["auto", "auto", "explicit-id"]

    assert await resolve(generic, None) == "gemma"
    assert await resolve(generic, "other") == "other"
    assert requested == ["auto", "auto", "explicit-id"]


@pytest.mark.asyncio
async def test_daemon_openai_tool_chat_adapter_never_sends_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolver(monkeypatch, "Qwen/Qwen2.5-VL-7B-Instruct")
    client = _FakeClient()
    monkeypatch.setattr(
        tool_chat_builder,
        "create_local_provider_adapter",
        lambda _endpoint: _FakeLocalAdapter(client),
    )
    adapter = _daemon_tool_chat_adapter_factories(_config())[AIAdapterStyle.OPENAI_COMPATIBLE]()

    result = await adapter.chat(
        ToolChatRequest(
            prompt="Summarize the repo.",
            tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
            project_path="/repo",
            model="auto",
        ),
        _binding("vllm-local", "vllm", "auto"),
    )

    assert [call["model"] for call in client.chat.completions.calls] == [
        "Qwen/Qwen2.5-VL-7B-Instruct"
    ]
    assert result.model == "Qwen/Qwen2.5-VL-7B-Instruct"
