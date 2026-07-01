"""Tests for the provider-agnostic tool_chat service dispatch path."""

from __future__ import annotations

import asyncio

import pytest

from gobby.ai import (
    AIAdapterStyle,
    AICapability,
    CapabilityBinding,
    CapabilityUnavailableError,
    build_daemon_ai_capability_registry,
    build_daemon_tool_chat_service,
)
from gobby.ai._text_generation_helpers import _CandidateTimeoutError
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolChatResult, ToolPolicy
from gobby.ai._tool_chat_service import ToolChatService
from gobby.config.ai import AIConfig, GenerationConfig, LocalGenerationConfig
from gobby.config.app import DaemonConfig

_POLICY = ToolPolicy(cli="gcode", tools=("search", "outline"))


class _RecordingAdapter:
    """A tool_chat adapter that records the binding it was dispatched with."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.bindings: list[CapabilityBinding] = []

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        self.bindings.append(binding)
        return ToolChatResult(text=f"narrative::{self.label}", tool_use_count=2, turns=1)


def _registry() -> object:
    return build_daemon_ai_capability_registry(
        DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    local=LocalGenerationConfig(
                        endpoints={
                            "lm-studio": {
                                "api_base": "http://localhost:1234/v1",
                                "model": "gemma",
                            },
                        }
                    )
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


def _request(**overrides: object) -> ToolChatRequest:
    base: dict[str, object] = {
        "prompt": "Document the auth module.",
        "tool_policy": _POLICY,
        "project_path": "/repo",
    }
    base.update(overrides)
    return ToolChatRequest(**base)  # type: ignore[arg-type]


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
    result = await service.chat_result(_request(candidates=("local:lm-studio/gemma",)))
    assert result.adapter_style == "openai_compatible"
    assert result.provider == "local:lm-studio"
    assert result.text == "narrative::openai_compatible"
    assert [b.provider for b in openai.bindings] == ["local:lm-studio"]
    assert llm.bindings == []


@pytest.mark.asyncio
async def test_same_path_skips_unavailable_candidate_then_dispatches_by_style() -> None:
    # codex maps to the daemon style (no tool_chat adapter yet): unavailable, so
    # selection moves to the next candidate. Switching which candidate wins
    # changes only the binding/adapter — the service path is identical.
    service, llm, openai = _service()
    result = await service.chat_result(_request(candidates=("codex/gpt-5.5", "claude/haiku")))
    assert result.adapter_style == "llm_provider"
    assert [b.provider for b in llm.bindings] == ["claude"]
    assert openai.bindings == []


@pytest.mark.asyncio
async def test_no_available_candidate_raises_without_fallback() -> None:
    service, llm, openai = _service()
    with pytest.raises(CapabilityUnavailableError):
        await service.chat_result(_request(candidates=("codex/gpt-5.5",)))
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


def test_tool_chat_candidate_timeout_selection_by_adapter_style() -> None:
    # Mirror TextGenerationService: spawn-cold lanes get the larger CLI timeout,
    # fast API lanes (and no binding) keep the tight candidate timeout.
    service = ToolChatService(
        _registry(),
        candidate_timeout_seconds=60.0,
        cli_candidate_timeout_seconds=150.0,
    )
    default_request = _request()

    def _binding(style: AIAdapterStyle) -> CapabilityBinding:
        return CapabilityBinding(
            capability=AICapability.TOOL_CHAT,
            provider="p",
            adapter_style=style,
            available=True,
        )

    for style in (
        AIAdapterStyle.CLI,
        AIAdapterStyle.DAEMON,
        AIAdapterStyle.LLM_PROVIDER,
        AIAdapterStyle.ACP,
    ):
        assert service._candidate_timeout_for_binding(default_request, _binding(style)) == 150.0
    for style in (AIAdapterStyle.LOCAL, AIAdapterStyle.OPENAI_COMPATIBLE):
        assert service._candidate_timeout_for_binding(default_request, _binding(style)) == 60.0
    assert service._candidate_timeout_for_binding(default_request, None) == 60.0


@pytest.mark.asyncio
async def test_tool_chat_times_out_slow_candidate_then_tries_next() -> None:
    # A spawn-cold candidate that blocks past cli_candidate_timeout_seconds fails;
    # selection moves to the next candidate instead of wedging the caller.
    slow = _SlowAdapter("llm_provider")
    fast = _RecordingAdapter("openai_compatible")
    service = ToolChatService(
        _registry(),
        adapters={
            AIAdapterStyle.LLM_PROVIDER: slow,
            AIAdapterStyle.OPENAI_COMPATIBLE: fast,
        },
        candidate_timeout_seconds=0.05,
        cli_candidate_timeout_seconds=0.05,
    )
    result = await service.chat_result(
        _request(candidates=("claude/haiku", "local:lm-studio/gemma"))
    )
    assert slow.calls == 1
    assert result.adapter_style == "openai_compatible"
    assert result.text == "narrative::openai_compatible"
    assert [b.provider for b in fast.bindings] == ["local:lm-studio"]


@pytest.mark.asyncio
async def test_single_timed_out_tool_chat_candidate_raises_timeout() -> None:
    slow = _SlowAdapter("llm_provider")
    service = ToolChatService(
        _registry(),
        adapters={AIAdapterStyle.LLM_PROVIDER: slow},
        cli_candidate_timeout_seconds=0.05,
    )
    with pytest.raises(_CandidateTimeoutError, match="timed out after"):
        await service.chat_result(_request(candidates=("claude/haiku",)))
    assert slow.calls == 1


def test_builder_threads_generation_timeouts_into_tool_chat_service() -> None:
    config = DaemonConfig(
        ai=AIConfig(
            generation=GenerationConfig(
                candidate_timeout_seconds=33.0,
                cli_candidate_timeout_seconds=99.0,
                timeout_seconds=600.0,
            )
        ),
    )
    service = build_daemon_tool_chat_service(config)
    assert service._candidate_timeout_seconds == 33.0
    assert service._cli_candidate_timeout_seconds == 600.0
