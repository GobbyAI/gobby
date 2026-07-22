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
from gobby.ai._tool_chat_builtins import BuiltinToolSpec
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolChatResult, ToolPolicy
from gobby.ai._tool_chat_service import ToolChatService
from gobby.config.ai import AIConfig, GenerationConfig, LocalGenerationConfig
from gobby.config.app import DaemonConfig

pytestmark = pytest.mark.unit

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
                                "tool_chat": True,
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

    async def handler(arguments: object, context: object) -> object:
        raise AssertionError("builtin handler must never run on a spawn-style binding")

    builtin = BuiltinToolSpec(
        name="read_page",
        description="Read a page.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,  # type: ignore[arg-type]
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


def test_tool_chat_candidate_timeout_is_attempt_budget_with_request_overrides() -> None:
    # A tool_chat candidate is a full multi-turn agentic run: every lane is
    # bounded by the overall attempt budget, not the single-generation candidate
    # budgets (gobby-#18285). Request-level overrides tighten per lane.
    service = ToolChatService(_registry(), attempt_timeout_seconds=600.0)
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
        AIAdapterStyle.LOCAL,
        AIAdapterStyle.OPENAI_COMPATIBLE,
    ):
        assert service._candidate_timeout_for_binding(default_request, _binding(style)) == 600.0
    assert service._candidate_timeout_for_binding(default_request, None) == 600.0

    # Spawn-cold lanes honor the request's cli_candidate override; fast lanes
    # honor candidate_timeout_seconds; neither override leaks to the other lane.
    override_request = _request(
        candidate_timeout_seconds=30.0,
        cli_candidate_timeout_seconds=150.0,
    )
    spawn_cold = _binding(AIAdapterStyle.LLM_PROVIDER)
    fast = _binding(AIAdapterStyle.OPENAI_COMPATIBLE)
    assert service._candidate_timeout_for_binding(override_request, spawn_cold) == 150.0
    assert service._candidate_timeout_for_binding(override_request, fast) == 30.0
    cli_only = _request(cli_candidate_timeout_seconds=150.0)
    assert service._candidate_timeout_for_binding(cli_only, fast) == 600.0


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
        attempt_timeout_seconds=0.05,
    )
    with pytest.raises(_CandidateTimeoutError, match="timed out after"):
        await service.chat_result(_request(candidates=("claude/haiku", "local:lm-studio/gemma")))
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
        await service.chat_result(_request(candidates=("claude/haiku", "local:lm-studio/gemma")))

    assert fast.bindings == []


@pytest.mark.asyncio
async def test_single_timed_out_tool_chat_candidate_raises_timeout() -> None:
    slow = _SlowAdapter("llm_provider")
    service = ToolChatService(
        _registry(),
        adapters={AIAdapterStyle.LLM_PROVIDER: slow},
        attempt_timeout_seconds=0.05,
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
    # tool_chat bounds each candidate (a full agentic run) by the overall
    # timeout_seconds attempt budget; the tight per-candidate budgets stay
    # text-generation-only for fast failover (gobby-#17710, gobby-#18285).
    assert service._attempt_timeout_seconds == 600.0
