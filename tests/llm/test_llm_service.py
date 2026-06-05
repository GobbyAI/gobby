"""Tests for the LLMService facade."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.ai import AIAdapterStyle, AICapability, AICapabilityRegistry, CapabilityBinding
from gobby.config.ai import AIConfig, GenerationConfig, LocalGenerationConfig
from gobby.config.app import DaemonConfig
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig
from gobby.config.sessions import DigestConfig
from gobby.llm.service import LLMService

pytestmark = pytest.mark.unit


class FakeTextGeneration:
    def __init__(self) -> None:
        self.requests = []
        self.registry = AICapabilityRegistry(
            [
                CapabilityBinding(
                    capability=AICapability.TEXT_GENERATE,
                    provider="claude",
                    adapter_style=AIAdapterStyle.LLM_PROVIDER,
                    available=True,
                    models=("haiku", "sonnet"),
                )
            ]
        )

    async def generate(self, request):
        self.requests.append(request)
        return "generated text"

    async def generate_json(self, request):
        self.requests.append(request)
        return {"ok": True}


@pytest.fixture
def llm_config() -> DaemonConfig:
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            claude=LLMProviderConfig(models="haiku,sonnet", default_model="sonnet")
        )
    )


def test_init_with_empty_providers_succeeds() -> None:
    service = LLMService(DaemonConfig(llm_providers=LLMProvidersConfig(claude=None)))

    assert service.initialized_providers == []


def test_get_provider_claude_caches_instance(llm_config: DaemonConfig) -> None:
    service = LLMService(llm_config)

    with patch("gobby.llm.claude.ClaudeLLMProvider") as mock_provider_class:
        mock_provider = MagicMock()
        mock_provider_class.return_value = mock_provider

        first = service.get_provider("claude")
        second = service.get_provider("claude")

    assert first is mock_provider
    assert second is mock_provider
    assert mock_provider_class.call_count == 1
    assert service.initialized_providers == ["claude"]


@pytest.mark.asyncio
async def test_call_feature_delegates_to_text_generation(llm_config: DaemonConfig) -> None:
    service = LLMService(llm_config)
    fake_generation = FakeTextGeneration()
    service._text_generation = fake_generation
    config = DigestConfig(candidates=["claude/haiku"])

    result = await service.call_feature(config, "prompt", system_prompt="system", caller="test")

    assert result == "generated text"
    request = fake_generation.requests[0]
    assert request.prompt == "prompt"
    assert request.system_prompt == "system"
    assert request.profile == "feature_low"
    assert request.candidates == ("claude/haiku",)
    assert request.caller == "test"


@pytest.mark.asyncio
async def test_call_json_feature_delegates_to_text_generation(llm_config: DaemonConfig) -> None:
    service = LLMService(llm_config)
    fake_generation = FakeTextGeneration()
    service._text_generation = fake_generation
    config = DigestConfig(candidates=["claude/haiku"])

    result = await service.call_json_feature(config, "prompt", system_prompt="system")

    assert result == {"ok": True}
    request = fake_generation.requests[0]
    assert request.profile == "feature_low"
    assert request.candidates == ("claude/haiku",)


def test_enabled_providers_reflects_text_generation_registry(llm_config: DaemonConfig) -> None:
    service = LLMService(llm_config)
    fake_generation = FakeTextGeneration()
    service._text_generation = fake_generation

    assert service.enabled_providers == ["claude"]


def test_get_provider_local_requires_generation_or_local_config(llm_config: DaemonConfig) -> None:
    service = LLMService(llm_config)

    with pytest.raises(ValueError, match="ai.generation.local"):
        service.get_provider("local")


def test_get_provider_local_uses_ai_generation_config() -> None:
    config = DaemonConfig(
        ai=AIConfig(
            generation=GenerationConfig(
                local=LocalGenerationConfig(
                    enabled=True,
                    api_base="http://localhost:1234/v1",
                    model="qwen-coder",
                )
            )
        )
    )
    service = LLMService(config)

    with patch("gobby.llm.local.LocalLLMProvider") as mock_provider_class:
        mock_provider = MagicMock()
        mock_provider_class.return_value = mock_provider

        provider = service.get_provider("local")

    assert provider is mock_provider
