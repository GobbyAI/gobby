"""Tests for the LLMService facade."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.ai import AIAdapterStyle, AICapability, AICapabilityRegistry, CapabilityBinding
from gobby.config.app import DaemonConfig
from gobby.config.sessions import DigestConfig
from gobby.llm import create_llm_service
from gobby.llm.service import LLMService

pytestmark = pytest.mark.unit


class FakeTextGeneration:
    def __init__(self, bindings: list[CapabilityBinding] | None = None) -> None:
        self.requests: list[Any] = []
        if bindings is None:
            bindings = [
                CapabilityBinding(
                    capability=AICapability.TEXT_GENERATE,
                    provider="claude",
                    adapter_style=AIAdapterStyle.LLM_PROVIDER,
                    available=True,
                    models=("haiku", "sonnet"),
                )
            ]
        self.registry = AICapabilityRegistry(bindings)

    async def generate(self, request: Any) -> str:
        self.requests.append(request)
        return "generated text"

    async def generate_json(self, request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return {"ok": True}


@pytest.fixture
def llm_config() -> DaemonConfig:
    return DaemonConfig()


def test_init_with_empty_providers_succeeds() -> None:
    service = LLMService(DaemonConfig(), text_generation=FakeTextGeneration(bindings=[]))

    assert repr(service) == "LLMService(enabled=[])"


def test_create_llm_service_uses_injected_text_generation(llm_config: DaemonConfig) -> None:
    fake_generation = FakeTextGeneration()

    service = create_llm_service(llm_config, text_generation=fake_generation)

    assert service.enabled_providers == ["claude"]


def test_direct_provider_accessors_are_not_public(llm_config: DaemonConfig) -> None:
    service = LLMService(llm_config, text_generation=FakeTextGeneration())

    assert not hasattr(service, "get_provider")
    assert not hasattr(service, "get_default_provider")
    assert not hasattr(service, "initialized_providers")


@pytest.mark.asyncio
async def test_call_feature_delegates_to_text_generation(llm_config: DaemonConfig) -> None:
    fake_generation = FakeTextGeneration()
    service = LLMService(llm_config, text_generation=fake_generation)
    config = DigestConfig(candidates=["claude/haiku"])

    result = await service.call_feature(
        config,
        "prompt",
        system_prompt="system",
        caller="test",
        cwd="/tmp/project",
    )

    assert result == "generated text"
    request = fake_generation.requests[0]
    assert request.prompt == "prompt"
    assert request.system_prompt == "system"
    assert request.profile == "feature_low"
    assert request.candidates == ("claude/haiku",)
    assert request.candidate_timeout_seconds is None
    assert request.caller == "test"
    assert request.cwd == "/tmp/project"


@pytest.mark.asyncio
async def test_call_json_feature_delegates_to_text_generation(llm_config: DaemonConfig) -> None:
    fake_generation = FakeTextGeneration()
    service = LLMService(llm_config, text_generation=fake_generation)
    config = DigestConfig(candidates=["claude/haiku"])

    result = await service.call_json_feature(
        config, "prompt", system_prompt="system", cwd="/tmp/project"
    )

    assert result == {"ok": True}
    request = fake_generation.requests[0]
    assert request.profile == "feature_low"
    assert request.candidates == ("claude/haiku",)
    assert request.cwd == "/tmp/project"


@pytest.mark.asyncio
async def test_call_json_feature_forwards_candidate_timeout(llm_config: DaemonConfig) -> None:
    fake_generation = FakeTextGeneration()
    service = LLMService(llm_config, text_generation=fake_generation)
    config = DigestConfig(candidates=["claude/haiku"], candidate_timeout_seconds=123.0)

    await service.call_json_feature(config, "prompt")

    request = fake_generation.requests[0]
    assert request.candidate_timeout_seconds == 123.0


def test_enabled_providers_reflects_text_generation_registry(llm_config: DaemonConfig) -> None:
    fake_generation = FakeTextGeneration()
    service = LLMService(llm_config, text_generation=fake_generation)

    assert service.enabled_providers == ["claude"]
