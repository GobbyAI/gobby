"""Tests for the provider-agnostic tool_chat service dispatch path."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    LIMIT_STOP_REASONS,
    ToolChatRequest,
    ToolChatResult,
    ToolLoopLimits,
    ToolPolicy,
)
from gobby.ai._tool_chat_service import ToolChatService
from gobby.config.ai import AIConfig, GenerationConfig, ToolLoopConfig
from gobby.config.app import DaemonConfig
from gobby.config.feature_base import FeatureCandidateConfig
from gobby.providers.capabilities.models import SpeedMode
from gobby.providers.capabilities.resolve import (
    ReasoningResolution,
    ReasoningStatus,
    SpeedResolution,
    SpeedStatus,
)

pytestmark = pytest.mark.unit

_POLICY = ToolPolicy(cli="gcode", tools=("search", "outline"))


@pytest.mark.asyncio
async def test_tool_request_cancellation_waits_for_outer_lease_revocation() -> None:
    lease_entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    revoked = asyncio.Event()

    @asynccontextmanager
    async def lease(
        request: ToolChatRequest,
        _timeout_seconds: float,
    ) -> AsyncIterator[ToolChatRequest]:
        try:
            lease_entered.set()
            yield replace(request, project_path="/authoritative/repo")
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
            revoked.set()

    slow = _SlowAdapter("lease-cancellation", delay=30.0)
    service = ToolChatService(
        _registry(),
        adapters={
            AIAdapterStyle.LLM_PROVIDER: slow,
            AIAdapterStyle.OPENAI_COMPATIBLE: slow,
        },
        lease_factory=lease,
    )
    task = asyncio.create_task(service.chat_result(_request()))
    await lease_entered.wait()

    task.cancel()
    await cleanup_started.wait()
    assert not task.done()
    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert revoked.is_set()


class _RecordingAdapter:
    """A tool_chat adapter that records the binding it was dispatched with."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.bindings: list[CapabilityBinding] = []
        self.requests: list[ToolChatRequest] = []
        self.result = ToolChatResult(text=f"narrative::{self.label}", tool_use_count=2, turns=1)

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        self.requests.append(request)
        self.bindings.append(binding)
        return self.result


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
            caller="gwiki.ask.deep",
            request_id="019fc08a-1d63-4b23-bbc8-659d56bc4168",
        ),
        **overrides,
    )


class _ReasoningResolver:
    def __init__(self, *, fail_on_reasoning: bool = False) -> None:
        self.fail_on_reasoning = fail_on_reasoning

    def resolve_reasoning(
        self,
        provider: str,
        model: str,
        effort: str | None,
        *,
        transport_supports_effort: bool,
    ) -> ReasoningResolution:
        if self.fail_on_reasoning:
            raise AssertionError("unset reasoning must skip resolution")
        assert (provider, model, effort, transport_supports_effort) == (
            "claude",
            "haiku",
            "auto",
            True,
        )
        return ReasoningResolution("auto", "medium", ReasoningStatus.VERIFIED, None)

    def resolve_route(
        self,
        provider: str,
        model: str,
        speed_mode: SpeedMode = SpeedMode.STANDARD,
        surface: str = "spawn-cli",
    ) -> SpeedResolution:
        return SpeedResolution(
            requested=speed_mode,
            effective=SpeedMode.STANDARD,
            status=SpeedStatus.STANDARD,
            selector=model,
            activations=(),
            reason=None,
        )


def test_tool_chat_request_exposes_effective_limits_and_shared_stop_reasons() -> None:
    default_request = _request()
    custom_limits = ToolLoopLimits(max_turns=3, tool_timeout_seconds=0.25)
    custom_request = _request(limits=custom_limits)

    assert default_request.effective_limits == ToolLoopLimits()
    assert custom_request.effective_limits is custom_limits
    assert LIMIT_STOP_REASONS == frozenset({"max_turns", "max_tool_calls", "timeout"})


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
async def test_tool_chat_resolves_auto_before_adapter() -> None:
    llm = _RecordingAdapter("llm_provider")
    service = ToolChatService(
        _registry(),
        adapters={AIAdapterStyle.LLM_PROVIDER: llm},
        capability_resolver=_ReasoningResolver(),
    )

    await service.chat_result(
        _request(
            candidates=(FeatureCandidateConfig(candidate="claude/haiku", reasoning_effort="auto"),)
        )
    )

    assert llm.requests[0].reasoning_effort == "medium"


@pytest.mark.asyncio
async def test_tool_chat_unset_reasoning_skips_resolution() -> None:
    llm = _RecordingAdapter("llm_provider")
    service = ToolChatService(
        _registry(),
        adapters={AIAdapterStyle.LLM_PROVIDER: llm},
        capability_resolver=_ReasoningResolver(fail_on_reasoning=True),
    )

    await service.chat_result(_request(candidates=("claude/haiku",)))

    assert llm.requests[0].reasoning_effort is None


@pytest.mark.asyncio
async def test_limit_logs_include_safe_correlation_routing_limits_and_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, llm, _openai = _service()
    llm.result = ToolChatResult(
        text="bounded narrative",
        tool_use_count=30,
        turns=7,
        tools={"gcode_search": 22, "gcode_outline": 8},
        stop_reason="max_tool_calls",
    )
    caplog.set_level(logging.DEBUG, logger="gobby.ai._tool_chat_service")

    result = await service.chat_result(
        _request(
            prompt="SENSITIVE PROMPT",
            project_path="/private/full/project/path",
            candidates=("claude/haiku",),
        )
    )

    assert result.tool_use_count == 30
    assert result.turns == 7
    assert [record.getMessage() for record in caplog.records] == [
        "tool_chat request started",
        "tool_chat request completed",
        "tool_chat terminated by limit",
    ]
    record = caplog.records[-1]
    fields = record.__dict__
    assert record.levelno == logging.INFO
    assert fields["caller"] == "gwiki.ask.deep"
    assert fields["request_id"] == "019fc08a-1d63-4b23-bbc8-659d56bc4168"
    assert fields["profile"] is None
    assert fields["requested_provider"] == "claude"
    assert fields["requested_model"] == "haiku"
    assert fields["provider"] == "claude"
    assert fields["model"] == "haiku"
    assert fields["adapter_style"] == "llm_provider"
    assert fields["stop_reason"] == "max_tool_calls"
    assert fields["max_turns"] is None
    assert fields["max_tool_calls"] == 30
    assert fields["max_bytes_per_tool_result"] == 16_384
    assert fields["tool_timeout_seconds"] == 300
    assert fields["loop_timeout_seconds"] == 1_200
    assert fields["turns"] == 7
    assert fields["tool_use_count"] == 30
    assert fields["tools"] == {"gcode_search": 22, "gcode_outline": 8}
    logged = repr([record.__dict__ for record in caplog.records])
    assert "SENSITIVE PROMPT" not in logged
    assert "/private/full/project/path" not in logged


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
async def test_no_available_candidate_raises_without_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, llm, openai = _service()
    caplog.set_level(logging.INFO, logger="gobby.ai._tool_chat_service")
    with pytest.raises(CapabilityUnavailableError):
        await service.chat_result(
            _request(
                prompt="SENSITIVE FAILURE PROMPT",
                project_path="/private/failure/path",
                candidates=("codex/gpt-5.6-terra",),
            )
        )
    # No adapter was invoked — no silent fallback to another provider/feature.
    assert llm.bindings == []
    assert openai.bindings == []
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "tool_chat capability unavailable"
    )
    fields = record.__dict__
    assert record.levelno == logging.INFO
    assert fields["caller"] == "gwiki.ask.deep"
    assert fields["request_id"] == "019fc08a-1d63-4b23-bbc8-659d56bc4168"
    assert fields["provider"] == "codex"
    assert fields["model"] == "gpt-5.6-terra"
    assert fields["stop_reason"] == "capability_unavailable"
    assert fields["max_tool_calls"] == 30
    logged = repr(record.__dict__)
    assert "SENSITIVE FAILURE PROMPT" not in logged
    assert "/private/failure/path" not in logged


class _SlowAdapter:
    """A tool_chat adapter that blocks past the candidate timeout."""

    def __init__(
        self,
        label: str,
        *,
        delay: float = 5.0,
        unavailable: bool = False,
    ) -> None:
        self.label = label
        self.delay = delay
        self.unavailable = unavailable
        self.calls = 0
        self.elapsed: list[float] = []

    async def chat(self, request: ToolChatRequest, binding: CapabilityBinding) -> ToolChatResult:
        self.calls += 1
        started = asyncio.get_running_loop().time()
        try:
            await asyncio.sleep(self.delay)
            if self.unavailable:
                raise CapabilityUnavailableError(
                    AICapability.TOOL_CHAT,
                    provider=binding.provider,
                    reason=f"{self.label} unavailable",
                )
        finally:
            self.elapsed.append(asyncio.get_running_loop().time() - started)
        return ToolChatResult(text=f"narrative::{self.label}", tool_use_count=0, turns=1)


class _FailingAdapter:
    async def chat(self, _request: ToolChatRequest, _binding: CapabilityBinding) -> ToolChatResult:
        raise RuntimeError("adapter broke")


@pytest.mark.asyncio
async def test_tool_chat_service_uses_one_canonical_request_deadline() -> None:
    first = _SlowAdapter("first", delay=0.35, unavailable=True)
    second = _SlowAdapter("second")
    service = ToolChatService(
        _registry(),
        adapters={
            AIAdapterStyle.LLM_PROVIDER: first,
            AIAdapterStyle.OPENAI_COMPATIBLE: second,
        },
    )
    started = asyncio.get_running_loop().time()

    with pytest.raises(_CandidateTimeoutError, match="timed out after 1s"):
        await service.chat_result(
            _request(
                candidates=("claude/haiku", "endpoint:lm-studio/gemma"),
                limits=ToolLoopLimits(loop_timeout_seconds=1),
            )
        )

    elapsed = asyncio.get_running_loop().time() - started
    assert first.calls == 1
    assert second.calls == 1
    assert 0.8 <= elapsed < 1.3
    assert first.elapsed[0] >= 0.3
    assert second.elapsed[0] < 0.85


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
