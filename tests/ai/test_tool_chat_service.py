"""Tests for the provider-agnostic tool_chat service dispatch path."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from gobby.ai import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityUnavailableError,
    build_daemon_ai_capability_registry,
    build_daemon_tool_chat_service,
)
from gobby.ai._text_generation_helpers import _CandidateTimeoutError
from gobby.ai._tool_chat_builtins import (
    BuiltinExecutionContext,
    BuiltinToolResult,
    BuiltinToolSpec,
)
from gobby.ai._tool_chat_contracts import (
    ToolChatRequest,
    ToolChatResult,
    ToolLoopLimits,
    ToolPolicy,
)
from gobby.ai._tool_chat_service import ToolChatService
from gobby.config.ai import AIConfig, GenerationConfig, ToolLoopConfig
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit

_POLICY = ToolPolicy(cli="gcode", tools=("search", "outline"))


class _RecordingAdapter:
    """A tool_chat adapter that records the binding it was dispatched with."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.bindings: list[CapabilityBinding] = []
        self.requests: list[ToolChatRequest] = []

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        self.requests.append(request)
        self.bindings.append(binding)
        return ToolChatResult(text=f"narrative::{self.label}", tool_use_count=2, turns=1)


def _registry() -> AICapabilityRegistry:
    return build_daemon_ai_capability_registry(
        DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "lm-studio": {
                            "api_base": "http://localhost:1234/v1",
                            "model": "gemma",
                            "tool_chat": True,
                        },
                    }
                )
            ),
        ),
        provider_installed=lambda _entry: True,
    )


def _service() -> tuple[ToolChatService, _RecordingAdapter, _RecordingAdapter]:
    llm = _RecordingAdapter("llm_provider")
    openai = _RecordingAdapter("openai_compatible")
    service = ToolChatService(
        _registry(),
        adapters={
            AIAdapterStyle.LLM_PROVIDER: llm,
            AIAdapterStyle.OPENAI_COMPATIBLE: openai,
        },
    )
    return service, llm, openai


def _request(**overrides: Any) -> ToolChatRequest:
    return replace(
        ToolChatRequest(
            prompt="Document the auth module.",
            tool_policy=_POLICY,
            project_path="/repo",
        ),
        **overrides,
    )


@pytest.mark.asyncio
async def test_llm_provider_candidate_dispatches_to_llm_provider_adapter() -> None:
    service, llm, openai = _service()
    result = await service.chat_result(_request(candidates=("claude/haiku",)))
    assert result.adapter_style == "llm_provider"
    assert result.provider == "claude"
    assert result.text == "narrative::llm_provider"
    assert [b.provider for b in llm.bindings] == ["claude"]
    assert openai.bindings == []


@pytest.mark.asyncio
async def test_openai_compatible_candidate_dispatches_to_openai_adapter() -> None:
    service, llm, openai = _service()
    result = await service.chat_result(_request(candidates=("endpoint:lm-studio/gemma",)))
    assert result.adapter_style == "openai_compatible"
    assert result.provider == "endpoint:lm-studio"
    assert result.text == "narrative::openai_compatible"
    assert [b.provider for b in openai.bindings] == ["endpoint:lm-studio"]
    assert llm.bindings == []


@pytest.mark.asyncio
async def test_tool_chat_normalizes_codex_endpoint_selector() -> None:
    adapter = _RecordingAdapter("daemon")
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TOOL_CHAT,
                provider="endpoint:openrouter",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("moonshotai/kimi-k3",),
            )
        ]
    )
    service = ToolChatService(
        registry,
        adapters={AIAdapterStyle.DAEMON: adapter},
    )

    result = await service.chat_result(
        _request(
            provider="codex",
            model="endpoint:openrouter/moonshotai/kimi-k3",
        )
    )

    assert result.provider == "endpoint:openrouter"
    assert result.model == "moonshotai/kimi-k3"
    assert adapter.requests[0].provider == "endpoint:openrouter"
    assert adapter.requests[0].model == "moonshotai/kimi-k3"


async def test_disallowed_adapter_style_is_capability_unavailable() -> None:
    service, _, _ = _service()

    with pytest.raises(CapabilityUnavailableError, match="disallowed"):
        await service.chat_result(
            _request(
                candidates=("claude/haiku",),
                allowed_adapter_styles=(AIAdapterStyle.OPENAI_COMPATIBLE,),
            )
        )


@pytest.mark.asyncio
async def test_builtins_on_spawn_style_binding_is_capability_unavailable() -> None:
    service, _, _ = _service()

    async def handler(
        arguments: dict[str, Any], context: BuiltinExecutionContext
    ) -> BuiltinToolResult:
        raise AssertionError("builtin handler must never run on a spawn-style binding")

    builtin = BuiltinToolSpec(
        name="read_page",
        description="Read a page.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )

    with pytest.raises(CapabilityUnavailableError, match="cannot execute builtin tools"):
        await service.chat_result(
            _request(candidates=("codex/gpt-5.6-terra",), builtins=(builtin,))
        )


@pytest.mark.asyncio
async def test_same_path_skips_unavailable_candidate_then_dispatches_by_style() -> None:
    # codex maps to the daemon style (no tool_chat adapter yet): unavailable, so
    # selection moves to the next candidate. Switching which candidate wins
    # changes only the binding/adapter — the service path is identical.
    service, llm, openai = _service()
    result = await service.chat_result(_request(candidates=("codex/gpt-5.6-terra", "claude/haiku")))
    assert result.adapter_style == "llm_provider"
    assert [b.provider for b in llm.bindings] == ["claude"]
    assert openai.bindings == []


@pytest.mark.asyncio
async def test_no_available_candidate_raises_without_fallback() -> None:
    service, llm, openai = _service()
    with pytest.raises(CapabilityUnavailableError):
        await service.chat_result(_request(candidates=("codex/gpt-5.6-terra",)))
    # No adapter was invoked — no silent fallback to another provider/feature.
    assert llm.bindings == []
    assert openai.bindings == []


class _SlowAdapter:
    """A tool_chat adapter that blocks past the candidate timeout."""

    def __init__(self, label: str, *, delay: float = 5.0) -> None:
        self.label = label
        self.delay = delay
        self.calls = 0

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return ToolChatResult(text=f"narrative::{self.label}", tool_use_count=0, turns=1)


class _FailingAdapter:
    async def chat(self, _request: ToolChatRequest, _binding: CapabilityBinding) -> ToolChatResult:
        raise RuntimeError("adapter broke")


def test_tool_chat_service_uses_one_canonical_request_deadline() -> None:
    limits = ToolLoopLimits(loop_timeout_seconds=600)
    service = ToolChatService(_registry(), default_limits=limits)

    assert service._default_limits == limits


@pytest.mark.asyncio
async def test_tool_chat_timeout_propagates_without_trying_next_candidate() -> None:
    slow = _SlowAdapter("llm_provider")
    fast = _RecordingAdapter("openai_compatible")
    service = ToolChatService(
        _registry(),
        adapters={
            AIAdapterStyle.LLM_PROVIDER: slow,
            AIAdapterStyle.OPENAI_COMPATIBLE: fast,
        },
        default_limits=ToolLoopLimits(loop_timeout_seconds=1),
    )
    with pytest.raises(_CandidateTimeoutError, match="timed out after"):
        await service.chat_result(_request(candidates=("claude/haiku", "endpoint:lm-studio/gemma")))
    assert slow.calls == 1
    assert fast.bindings == []


@pytest.mark.asyncio
async def test_tool_chat_runtime_error_propagates_without_trying_next_candidate() -> None:
    fast = _RecordingAdapter("openai_compatible")
    service = ToolChatService(
        _registry(),
        adapters={
            AIAdapterStyle.LLM_PROVIDER: _FailingAdapter(),
            AIAdapterStyle.OPENAI_COMPATIBLE: fast,
        },
    )

    with pytest.raises(RuntimeError, match="adapter broke"):
        await service.chat_result(_request(candidates=("claude/haiku", "endpoint:lm-studio/gemma")))

    assert fast.bindings == []


@pytest.mark.asyncio
async def test_single_timed_out_tool_chat_candidate_raises_timeout() -> None:
    slow = _SlowAdapter("llm_provider")
    service = ToolChatService(
        _registry(),
        adapters={AIAdapterStyle.LLM_PROVIDER: slow},
        default_limits=ToolLoopLimits(loop_timeout_seconds=1),
    )
    with pytest.raises(_CandidateTimeoutError, match="timed out after"):
        await service.chat_result(_request(candidates=("claude/haiku",)))
    assert slow.calls == 1


def test_builder_threads_tool_loop_limits_into_tool_chat_service() -> None:
    config = DaemonConfig(
        ai=AIConfig(
            generation=GenerationConfig(
                candidate_timeout_seconds=33.0,
                cli_candidate_timeout_seconds=99.0,
                timeout_seconds=600.0,
                tool_loop=ToolLoopConfig(
                    max_turns=17,
                    max_tool_calls=19,
                    max_bytes_per_tool_result=20_000,
                    tool_timeout_seconds=21,
                    loop_timeout_seconds=601,
                ),
            )
        ),
    )
    service = build_daemon_tool_chat_service(config)
    assert service._default_limits == ToolLoopLimits(
        max_turns=17,
        max_tool_calls=19,
        max_bytes_per_tool_result=20_000,
        tool_timeout_seconds=21,
        loop_timeout_seconds=601,
    )
