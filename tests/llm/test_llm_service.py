"""Tests for the LLMService facade."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.ai import AIAdapterStyle, AICapability, AICapabilityRegistry, CapabilityBinding
from gobby.config.app import DaemonConfig
from gobby.config.persistence import MemoryKnowledgeGraphConfig
from gobby.config.sessions import SessionSummaryConfig
from gobby.config.tasks import TaskValidationConfig
from gobby.llm import create_llm_service
from gobby.llm.service import LLMService

pytestmark = pytest.mark.unit

JSON_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


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
    config = SessionSummaryConfig(candidates=["claude/haiku"])

    def validate_output(text: str) -> str | None:
        return None if text else "empty"

    result = await service.call_feature(
        config,
        "prompt",
        system_prompt="system",
        caller="test",
        cwd="/tmp/project",
        output_validator=validate_output,
    )

    assert result == "generated text"
    request = fake_generation.requests[0]
    assert request.prompt == "prompt"
    assert request.system_prompt == "system"
    assert request.profile == "feature_low"
    assert len(request.candidates) == 1
    assert request.candidates[0].candidate == "claude/haiku"
    assert request.candidates[0].reasoning_effort is None
    assert request.caller == "test"
    assert request.cwd == "/tmp/project"
    assert request.output_validator is validate_output


@pytest.mark.asyncio
async def test_call_json_feature_delegates_to_text_generation(llm_config: DaemonConfig) -> None:
    fake_generation = FakeTextGeneration()
    service = LLMService(llm_config, text_generation=fake_generation)
    config = TaskValidationConfig(candidates=["claude/haiku"])

    result = await service.call_json_feature(
        config,
        "prompt",
        system_prompt="system",
        json_schema=JSON_SCHEMA,
        max_tokens=321,
        cwd="/tmp/project",
    )

    assert result == {"ok": True}
    request = fake_generation.requests[0]
    assert request.profile == "feature_mid"
    assert len(request.candidates) == 1
    assert request.candidates[0].candidate == "claude/haiku"
    assert request.candidates[0].reasoning_effort is None
    assert request.cwd == "/tmp/project"
    assert request.max_tokens == 321
    assert request.candidate_timeout_seconds is None
    assert request.cli_candidate_timeout_seconds is None
    assert request.json_schema == JSON_SCHEMA


@pytest.mark.asyncio
async def test_call_json_feature_preserves_structured_candidate_reasoning(
    llm_config: DaemonConfig,
) -> None:
    fake_generation = FakeTextGeneration()
    service = LLMService(llm_config, text_generation=fake_generation)
    config = MemoryKnowledgeGraphConfig(
        candidates=[{"candidate": "codex/gpt-5.6-sol", "reasoning_effort": "xhigh"}],
    )

    result = await service.call_json_feature(config, "prompt", json_schema=JSON_SCHEMA)

    assert result == {"ok": True}
    request = fake_generation.requests[0]
    assert len(request.candidates) == 1
    candidate = request.candidates[0]
    assert candidate.candidate == "codex/gpt-5.6-sol"
    assert candidate.reasoning_effort == "xhigh"


def test_enabled_providers_reflects_text_generation_registry(llm_config: DaemonConfig) -> None:
    fake_generation = FakeTextGeneration()
    service = LLMService(llm_config, text_generation=fake_generation)

    assert service.enabled_providers == ["claude"]
