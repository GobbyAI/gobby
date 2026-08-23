from __future__ import annotations

import pytest

from gobby.ai import AIAdapterStyle, AICapability, AICapabilityRegistry, CapabilityBinding
from gobby.ai._tool_chat_contracts import ToolChatRequest, ToolChatResult, ToolPolicy
from gobby.ai._tool_chat_service import ToolChatService
from gobby.providers.capabilities.models import ActivationDescriptor, SpeedMode
from gobby.providers.capabilities.resolve import (
    ReasoningResolution,
    ReasoningStatus,
    SpeedResolution,
    SpeedStatus,
)

pytestmark = pytest.mark.unit


class _Resolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, SpeedMode, str]] = []

    def resolve_route(
        self,
        provider: str,
        model: str,
        speed_mode: SpeedMode = SpeedMode.STANDARD,
        surface: str = "spawn-cli",
    ) -> SpeedResolution:
        self.calls.append((provider, model, speed_mode, surface))
        return SpeedResolution(
            requested=speed_mode,
            effective=SpeedMode.FAST,
            status=SpeedStatus.FAST_CONFIGURED,
            selector=model,
            activations=(
                ActivationDescriptor(
                    "request_parameter",
                    "app-server",
                    {"name": "serviceTier", "value": "priority"},
                ),
            ),
            reason=None,
        )

    def resolve_reasoning(
        self,
        provider: str,
        model: str,
        effort: str | None,
        *,
        transport_supports_effort: bool,
    ) -> ReasoningResolution:
        return ReasoningResolution(effort, effort, ReasoningStatus.VERIFIED, None)


class _Adapter:
    def __init__(self) -> None:
        self.requests: list[ToolChatRequest] = []

    async def chat(
        self,
        request: ToolChatRequest,
        binding: CapabilityBinding,
    ) -> ToolChatResult:
        self.requests.append(request)
        return ToolChatResult(
            text="done",
            provider=binding.provider,
            model=request.model,
            response_metadata={"serviceTier": "priority"},
        )


@pytest.mark.asyncio
async def test_codex_tier_activation() -> None:
    resolver = _Resolver()
    adapter = _Adapter()
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TOOL_CHAT,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-test",),
            )
        ]
    )
    service = ToolChatService(
        registry,
        adapters={AIAdapterStyle.DAEMON: adapter},
        capability_resolver=resolver,
    )

    result = await service.chat_result(
        ToolChatRequest(
            prompt="inspect",
            tool_policy=ToolPolicy(cli="gcode", tools=("search",)),
            project_path="/repo",
            provider="codex",
            model="gpt-test",
            speed_mode="fast",
        )
    )

    assert resolver.calls == [("codex", "gpt-test", SpeedMode.FAST, "app-server")]
    assert dict(adapter.requests[0].request_parameters) == {"serviceTier": "priority"}
    assert result.speed == {
        "requested": "fast",
        "effective": "fast",
        "status": "fast_applied",
        "reason": None,
    }
