from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

import gobby.ai._text_generation_adapters as text_generation_adapters
from gobby.adapters.acp_client import StreamEvent
from gobby.ai import (
    AgyCLITextGenerateAdapter,
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityUnavailableError,
    ClaudeTextGenerateAdapter,
    CodexCLITextGenerateAdapter,
    DroidCLITextGenerateAdapter,
    LocalTextGenerateAdapter,
    TextGenerationRequest,
    TextGenerationService,
    build_daemon_text_generation_service,
)
from gobby.ai._text_generation_builder import _daemon_text_generation_adapter_factories
from gobby.ai._text_generation_helpers import _CandidateTimeoutError, _coerce_text_result
from gobby.ai.text_generation import (
    ONE_SHOT_DIRECTIVE,
    FeatureGenerationUnavailableError,
    is_feature_generation_infrastructure_error,
)
from gobby.config.app import DaemonConfig
from gobby.config.feature_base import FeatureCandidateConfig, FeatureProfile
from gobby.llm.base import LLMProviderCancellation, LLMTextResult

pytestmark = pytest.mark.unit

TEXT_GENERATION_LOGGER = "gobby.ai.text_generation"


class RecordingAdapter:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.requests: list[TextGenerationRequest] = []

    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        return f"{self.provider}:{request.prompt}"


class StaticTextAdapter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[TextGenerationRequest] = []

    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        return self.text


class UsageAdapter:
    def __init__(self) -> None:
        self.requests: list[TextGenerationRequest] = []

    async def generate(self, request: TextGenerationRequest) -> LLMTextResult:
        self.requests.append(request)
        return LLMTextResult(
            text="Generated text",
            usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        )


class FailingAdapter:
    def __init__(self, message: str = "boom") -> None:
        self.message = message
        self.requests: list[TextGenerationRequest] = []

    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        raise RuntimeError(self.message)


class SlowAdapter:
    def __init__(self, delay: float = 30.0) -> None:
        self.delay = delay
        self.requests: list[TextGenerationRequest] = []

    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        return "slow text"

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        return {"slow": True}


class GateProbeState:
    def __init__(self, expected_started: int) -> None:
        self.expected_started = expected_started
        self.release = asyncio.Event()
        self.started_event = asyncio.Event()
        self.over_expected_event = asyncio.Event()
        self.lock = asyncio.Lock()
        self.started: list[str] = []
        self.active = 0
        self.max_active = 0

    async def enter(self, provider: str) -> None:
        async with self.lock:
            self.started.append(provider)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if len(self.started) >= self.expected_started:
                self.started_event.set()
            if self.active > self.expected_started:
                self.over_expected_event.set()

    async def exit(self) -> None:
        async with self.lock:
            self.active -= 1


class GateProbeAdapter:
    def __init__(
        self,
        state: GateProbeState,
        *,
        delays: dict[str, float] | None = None,
        failures: set[str] | None = None,
        wait_prompts: set[str] | None = None,
    ) -> None:
        self._state = state
        self._delays = delays or {}
        self._failures = failures or set()
        self._wait_prompts = wait_prompts or set()

    async def generate(self, request: TextGenerationRequest) -> str:
        provider = request.provider or "unknown"
        await self._state.enter(provider)
        try:
            if request.prompt in self._failures:
                raise RuntimeError("boom")
            if delay := self._delays.get(request.prompt):
                await asyncio.sleep(delay)
            if request.prompt in self._wait_prompts:
                await self._state.release.wait()
            return f"{provider}:{request.prompt}"
        finally:
            await self._state.exit()


class ProviderFailureAdapter:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.requests: list[TextGenerationRequest] = []

    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        raise self.error

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        self.requests.append(request)
        raise self.error


class JSONAdapter(RecordingAdapter):
    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        self.requests.append(request)
        return {"provider": self.provider, "model": request.model}


class JSONTextAdapter(RecordingAdapter):
    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        return '```json\n{"ok": true, "model": "%s"}\n```' % (request.model or "")


class EmptyTextAdapter(RecordingAdapter):
    async def generate(self, request: TextGenerationRequest) -> str:
        self.requests.append(request)
        return ""


@pytest.mark.asyncio
async def test_text_generation_service_selects_available_registry_binding() -> None:
    providers = {
        "claude": AIAdapterStyle.LLM_PROVIDER,
        "codex": AIAdapterStyle.DAEMON,
        "local:lm-studio": AIAdapterStyle.OPENAI_COMPATIBLE,
        "grok": AIAdapterStyle.CLI,
        "qwen": AIAdapterStyle.CLI,
        "droid": AIAdapterStyle.CLI,
    }
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider=provider,
                adapter_style=adapter_style,
                available=True,
            )
            for provider, adapter_style in providers.items()
        ]
    )
    adapters = {provider: RecordingAdapter(provider) for provider in providers}
    service = TextGenerationService(registry, adapters)

    for provider in providers:
        response = await service.generate(
            TextGenerationRequest(
                prompt="summarize",
                provider=provider,
                model=f"{provider}-model",
            )
        )
        assert response == f"{provider}:summarize"
        assert adapters[provider].requests[-1].provider == provider
        assert adapters[provider].requests[-1].model == f"{provider}-model"


@pytest.mark.asyncio
async def test_text_generation_service_generate_result_preserves_usage() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
            )
        ]
    )
    adapter = UsageAdapter()
    service = TextGenerationService(registry, {"local:lm-studio": adapter})

    result = await service.generate_result(
        TextGenerationRequest(prompt="summarize", provider="local:lm-studio", model="local-model")
    )

    assert result.text == "Generated text"
    assert result.usage == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    assert result.provider == "local:lm-studio"
    assert result.model == "local-model"
    assert adapter.requests[-1].prompt == "summarize"


@pytest.mark.asyncio
async def test_successful_text_generation_omits_feature_llm_call_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
            )
        ]
    )
    service = TextGenerationService(
        registry,
        {"local:lm-studio": RecordingAdapter("local:lm-studio")},
    )
    caplog.set_level(logging.INFO, logger=TEXT_GENERATION_LOGGER)

    await service.generate_result(
        TextGenerationRequest(prompt="summarize", provider="local:lm-studio", model="local-model")
    )

    assert [record for record in caplog.records if record.getMessage() == "feature_llm_call"] == []


@pytest.mark.asyncio
async def test_successful_text_generation_logs_feature_llm_call_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
            )
        ]
    )
    service = TextGenerationService(
        registry,
        {"local:lm-studio": RecordingAdapter("local:lm-studio")},
    )
    caplog.set_level(logging.DEBUG, logger=TEXT_GENERATION_LOGGER)

    await service.generate_result(
        TextGenerationRequest(prompt="summarize", provider="local:lm-studio", model="local-model")
    )

    records = [record for record in caplog.records if record.getMessage() == "feature_llm_call"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].success is True


@pytest.mark.asyncio
async def test_recoverable_candidate_failure_logs_feature_llm_call_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:bad",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("bad-model",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:good",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("good-model",),
            ),
        ]
    )
    service = TextGenerationService(
        registry,
        {
            "local:bad": FailingAdapter("temporary"),
            "local:good": RecordingAdapter("local:good"),
        },
    )
    caplog.set_level(logging.DEBUG, logger=TEXT_GENERATION_LOGGER)

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            candidates=("local:bad/bad-model", "local:good/good-model"),
        )
    )

    assert result.provider == "local:good"
    records = [record for record in caplog.records if record.getMessage() == "feature_llm_call"]
    assert [record.levelno for record in records] == [logging.DEBUG, logging.DEBUG]
    assert records[0].success is False
    assert records[1].success is True


@pytest.mark.asyncio
async def test_failed_text_generation_logs_feature_llm_call_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
            )
        ]
    )
    service = TextGenerationService(registry, {"local:lm-studio": FailingAdapter("boom")})
    caplog.set_level(logging.ERROR, logger=TEXT_GENERATION_LOGGER)

    with pytest.raises(RuntimeError, match="boom"):
        await service.generate_result(
            TextGenerationRequest(
                prompt="summarize", provider="local:lm-studio", model="local-model"
            )
        )

    records = [record for record in caplog.records if record.getMessage() == "feature_llm_call"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].success is False


@pytest.mark.asyncio
async def test_text_generation_service_falls_back_across_profile_candidates() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-local",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("haiku",),
            ),
        ]
    )
    local = FailingAdapter()
    claude = RecordingAdapter("claude")
    service = TextGenerationService(registry, {"local:lm-studio": local, "claude": claude})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            profile="feature_low",
            candidates=("local:lm-studio/qwen-local", "claude/haiku"),
        )
    )

    assert result.text == "claude:summarize"
    assert result.provider == "claude"
    assert result.model == "haiku"
    assert local.requests[0].model == "qwen-local"
    assert claude.requests[0].model == "haiku"


@pytest.mark.asyncio
async def test_text_generation_service_propagates_provider_cancellation() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("haiku",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.4-mini",),
            ),
        ]
    )
    claude = ProviderFailureAdapter(LLMProviderCancellation("shutdown"))
    codex = RecordingAdapter("codex")
    service = TextGenerationService(registry, {"claude": claude, "codex": codex})

    with pytest.raises(LLMProviderCancellation, match="shutdown"):
        await service.generate_result(
            TextGenerationRequest(
                prompt="summarize",
                profile="feature_low",
                candidates=("claude/haiku", "codex/gpt-5.4-mini"),
            )
        )

    assert claude.requests[0].model == "haiku"
    assert codex.requests == []


@pytest.mark.asyncio
async def test_text_generation_service_falls_back_when_candidate_returns_blank_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-local",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("haiku",),
            ),
        ]
    )
    claude = RecordingAdapter("claude")
    service = TextGenerationService(
        registry,
        {"local:lm-studio": StaticTextAdapter("   "), "claude": claude},
    )
    caplog.set_level(logging.DEBUG, logger=TEXT_GENERATION_LOGGER)

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            profile="feature_low",
            candidates=("local:lm-studio/qwen-local", "claude/haiku"),
        )
    )

    assert result.text == "claude:summarize"
    records = [record for record in caplog.records if record.getMessage() == "feature_llm_call"]
    assert [record.success for record in records] == [False, True]


@pytest.mark.asyncio
async def test_text_generation_service_falls_back_when_candidate_echoes_prompt() -> None:
    prompt = "Summarize this module once from lower-level summaries."
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.ACP,
                available=True,
                models=("qwen-model",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("sonnet",),
            ),
        ]
    )
    qwen = StaticTextAdapter(prompt)
    claude = RecordingAdapter("claude")
    service = TextGenerationService(registry, {"qwen": qwen, "claude": claude})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt=prompt,
            profile="feature_mid",
            candidates=("qwen/qwen-model", "claude/sonnet"),
        )
    )

    assert result.text == f"claude:{prompt}"
    assert result.provider == "claude"
    assert result.model == "sonnet"
    assert qwen.requests[0].model == "qwen-model"
    assert claude.requests[0].model == "sonnet"


@pytest.mark.asyncio
async def test_text_generation_service_falls_back_when_long_output_starts_with_prompt() -> None:
    prompt = "Summarize this module once. " + ("Keep the API details precise. " * 12)
    system_prompt = "You write concise module overviews."
    echoed_prefix = f"{system_prompt}\n\n{prompt}"
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.ACP,
                available=True,
                models=("qwen-model",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("sonnet",),
            ),
        ]
    )
    qwen = StaticTextAdapter(f"{echoed_prefix}\n\nGenerated prose after an echoed prompt.")
    claude = RecordingAdapter("claude")
    service = TextGenerationService(registry, {"qwen": qwen, "claude": claude})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            profile="feature_mid",
            candidates=("qwen/qwen-model", "claude/sonnet"),
        )
    )

    assert result.text == f"claude:{prompt}"
    assert result.provider == "claude"
    assert result.model == "sonnet"
    assert qwen.requests[0].model == "qwen-model"
    assert claude.requests[0].model == "sonnet"


@pytest.mark.asyncio
async def test_text_generation_service_rejects_single_candidate_echo() -> None:
    prompt = "Summarize this module once from lower-level summaries."
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.3-codex-spark",),
            )
        ]
    )
    codex = StaticTextAdapter(prompt)
    service = TextGenerationService(registry, {"codex": codex})

    with pytest.raises(RuntimeError, match="returned the prompt"):
        await service.generate_result(
            TextGenerationRequest(
                prompt=prompt,
                provider="codex",
                model="gpt-5.3-codex-spark",
            )
        )

    assert codex.requests[0].model == "gpt-5.3-codex-spark"


@pytest.mark.asyncio
async def test_text_generation_service_falls_back_between_named_local_endpoints() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-lm",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:ollama",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-ollama",),
            ),
        ]
    )
    lm_studio = FailingAdapter("lm studio offline")
    ollama = RecordingAdapter("local:ollama")
    service = TextGenerationService(
        registry,
        {"local:lm-studio": lm_studio, "local:ollama": ollama},
    )

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            profile="feature_low",
            candidates=("local:lm-studio/qwen-lm", "local:ollama/qwen-ollama"),
        )
    )

    assert result.text == "local:ollama:summarize"
    assert result.provider == "local:ollama"
    assert result.model == "qwen-ollama"
    assert lm_studio.requests[0].provider == "local:lm-studio"
    assert lm_studio.requests[0].model == "qwen-lm"
    assert ollama.requests[0].provider == "local:ollama"
    assert ollama.requests[0].model == "qwen-ollama"


@pytest.mark.asyncio
async def test_text_generation_service_routes_named_local_candidate_with_slashed_model_id() -> None:
    model = "qwen/qwen3-coder-30b"
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=(model,),
            ),
        ]
    )
    local = RecordingAdapter("local:lm-studio")
    service = TextGenerationService(registry, {"local:lm-studio": local})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            profile="feature_low",
            candidates=(f"local:lm-studio/{model}",),
        )
    )

    assert result.text == "local:lm-studio:summarize"
    assert result.provider == "local:lm-studio"
    assert result.model == model
    assert local.requests[0].provider == "local:lm-studio"
    assert local.requests[0].model == model


@pytest.mark.asyncio
async def test_text_generation_service_rejects_bare_local_candidate() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "local",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                reason="Use a named local generation endpoint provider such as local:lm-studio.",
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:ollama",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-ollama",),
            ),
        ]
    )
    ollama = RecordingAdapter("local:ollama")
    service = TextGenerationService(registry, {"local:ollama": ollama})

    with pytest.raises(CapabilityUnavailableError, match="named local generation endpoint"):
        await service.generate_result(
            TextGenerationRequest(
                prompt="summarize",
                profile="feature_low",
                candidates=("local/qwen-ollama",),
            )
        )

    assert ollama.requests == []


@pytest.mark.asyncio
async def test_text_generation_service_explicit_provider_model_bypasses_profile_defaults() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.3-codex-spark",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-local",),
            ),
        ]
    )
    codex = RecordingAdapter("codex")
    local = RecordingAdapter("local:lm-studio")
    service = TextGenerationService(registry, {"codex": codex, "local:lm-studio": local})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            profile="feature_low",
            provider="local:lm-studio",
            model="qwen-local",
        )
    )

    assert result.text == "local:lm-studio:summarize"
    assert result.provider == "local:lm-studio"
    assert result.model == "qwen-local"
    assert codex.requests == []
    assert local.requests[0].profile == "feature_low"
    assert local.requests[0].model == "qwen-local"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("local", None),
        (None, "qwen/qwen3.6-35b-a3b"),
    ],
)
async def test_text_generation_service_rejects_partial_explicit_routing(
    provider: str | None,
    model: str | None,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen/qwen3.6-35b-a3b",),
            )
        ]
    )
    local = RecordingAdapter("local:lm-studio")
    service = TextGenerationService(registry, {"local:lm-studio": local})

    with pytest.raises(ValueError, match="provider and model must be supplied together"):
        await service.generate_result(
            TextGenerationRequest(prompt="summarize", provider=provider, model=model)
        )

    assert local.requests == []


@pytest.mark.asyncio
async def test_text_generation_service_model_only_qwen_never_initializes_droid() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="droid",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen/qwen3.6-35b-a3b",),
            )
        ]
    )
    initialized: list[str] = []

    def droid_factory() -> RecordingAdapter:
        initialized.append("droid")
        return RecordingAdapter("droid")

    service = TextGenerationService(registry, adapter_factories={"droid": droid_factory})

    with pytest.raises(ValueError, match="provider and model must be supplied together"):
        await service.generate_result(
            TextGenerationRequest(prompt="summarize", model="qwen/qwen3.6-35b-a3b")
        )

    assert initialized == []


@pytest.mark.asyncio
async def test_text_generation_service_profile_only_expands_profile_defaults() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.4-mini",),
            ),
        ]
    )
    codex = RecordingAdapter("codex")
    service = TextGenerationService(
        registry,
        {"codex": codex},
        profile_defaults={
            FeatureProfile.HIGH: (
                FeatureCandidateConfig(candidate="codex/gpt-5.4", reasoning_effort="xhigh"),
            )
        },
    )

    result = await service.generate_result(
        TextGenerationRequest(prompt="summarize", profile="feature_low")
    )

    assert result.text == "codex:summarize"
    assert result.provider == "codex"
    assert result.model == "gpt-5.4-mini"
    assert codex.requests == [
        TextGenerationRequest(
            prompt="summarize",
            provider="codex",
            profile="feature_low",
            model="gpt-5.4-mini",
        )
    ]


@pytest.mark.asyncio
async def test_text_generation_service_profile_only_uses_configured_profile_defaults() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.4-mini",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-local",),
            ),
        ]
    )
    config = DaemonConfig(
        ai={
            "generation": {
                "profile_defaults": {
                    FeatureProfile.LOW: [
                        {
                            "candidate": "local:lm-studio/qwen-local",
                            "reasoning_effort": "auto",
                        }
                    ],
                }
            }
        }
    )
    codex = RecordingAdapter("codex")
    local = RecordingAdapter("local:lm-studio")
    service = TextGenerationService(
        registry,
        {"codex": codex, "local:lm-studio": local},
        profile_defaults=config.ai.generation.profile_defaults,
    )

    result = await service.generate_result(
        TextGenerationRequest(prompt="summarize", profile="feature_low")
    )

    assert result.text == "local:lm-studio:summarize"
    assert result.provider == "local:lm-studio"
    assert result.model == "qwen-local"
    assert codex.requests == []
    assert local.requests == [
        TextGenerationRequest(
            prompt="summarize",
            provider="local:lm-studio",
            profile="feature_low",
            model="qwen-local",
        )
    ]


@pytest.mark.asyncio
async def test_text_generation_service_applies_candidate_reasoning_effort_to_text_result() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.4",),
            ),
        ]
    )
    codex = RecordingAdapter("codex")
    service = TextGenerationService(
        registry,
        {"codex": codex},
        profile_defaults={
            FeatureProfile.HIGH: (
                FeatureCandidateConfig(candidate="codex/gpt-5.4", reasoning_effort="xhigh"),
            )
        },
    )

    result = await service.generate_result(
        TextGenerationRequest(prompt="summarize", profile=FeatureProfile.HIGH.value)
    )

    assert result.applied_reasoning_effort == "xhigh"
    assert codex.requests == [
        TextGenerationRequest(
            prompt="summarize",
            provider="codex",
            profile=FeatureProfile.HIGH.value,
            model="gpt-5.4",
            reasoning_effort="xhigh",
        )
    ]


@pytest.mark.asyncio
async def test_text_generation_service_request_reasoning_effort_overrides_candidate() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.5",),
            ),
        ]
    )
    codex = RecordingAdapter("codex")
    service = TextGenerationService(registry, {"codex": codex})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            candidates=(
                FeatureCandidateConfig(candidate="codex/gpt-5.5", reasoning_effort="xhigh"),
            ),
            reasoning_effort="low",
        )
    )

    assert result.applied_reasoning_effort == "low"
    assert codex.requests == [
        TextGenerationRequest(
            prompt="summarize",
            provider="codex",
            candidates=(
                FeatureCandidateConfig(candidate="codex/gpt-5.5", reasoning_effort="xhigh"),
            ),
            model="gpt-5.5",
            reasoning_effort="low",
        )
    ]


@pytest.mark.asyncio
async def test_text_generation_service_json_applies_effort_without_wrapping_result() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.5",),
            ),
        ]
    )
    codex = JSONAdapter("codex")
    service = TextGenerationService(registry, {"codex": codex})

    result = await service.generate_json(
        TextGenerationRequest(
            prompt="classify",
            candidates=(
                FeatureCandidateConfig(candidate="codex/gpt-5.5", reasoning_effort="xhigh"),
            ),
        )
    )

    assert result == {"provider": "codex", "model": "gpt-5.5"}
    assert codex.requests == [
        TextGenerationRequest(
            prompt="classify",
            provider="codex",
            candidates=(
                FeatureCandidateConfig(candidate="codex/gpt-5.5", reasoning_effort="xhigh"),
            ),
            model="gpt-5.5",
            reasoning_effort="xhigh",
        )
    ]


@pytest.mark.asyncio
async def test_text_generation_service_normalizes_auto_reasoning_effort_to_unset() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.5",),
            ),
        ]
    )
    codex = RecordingAdapter("codex")
    service = TextGenerationService(registry, {"codex": codex})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            candidates=(
                FeatureCandidateConfig(candidate="codex/gpt-5.5", reasoning_effort="xhigh"),
            ),
            reasoning_effort=" AUTO ",
        )
    )

    assert result.applied_reasoning_effort is None
    assert codex.requests == [
        TextGenerationRequest(
            prompt="summarize",
            provider="codex",
            candidates=(
                FeatureCandidateConfig(candidate="codex/gpt-5.5", reasoning_effort="xhigh"),
            ),
            model="gpt-5.5",
            reasoning_effort=None,
        )
    ]


@pytest.mark.asyncio
async def test_text_generation_service_accepts_valid_effort_for_reasoning_provider() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("codex-model",),
            ),
        ]
    )
    codex = RecordingAdapter("codex")
    service = TextGenerationService(registry, {"codex": codex})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            candidates=(
                FeatureCandidateConfig(candidate="codex/codex-model", reasoning_effort="high"),
            ),
        )
    )

    assert result.provider == "codex"
    assert result.applied_reasoning_effort == "high"
    assert codex.requests[0].reasoning_effort == "high"


@pytest.mark.asyncio
async def test_text_generation_service_rejects_known_effort_when_provider_efforts_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen-model",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="droid",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("droid-model",),
            ),
        ]
    )
    qwen = RecordingAdapter("qwen")
    droid = RecordingAdapter("droid")
    service = TextGenerationService(registry, {"qwen": qwen, "droid": droid})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            candidates=(
                FeatureCandidateConfig(candidate="qwen/qwen-model", reasoning_effort="high"),
                FeatureCandidateConfig(candidate="droid/droid-model", reasoning_effort="high"),
            ),
        )
    )

    assert result.provider == "droid"
    assert not qwen.requests
    assert droid.requests[0].reasoning_effort == "high"
    assert (
        "Unsupported reasoning_effort 'high' for provider 'qwen'; accepted: <none>" in caplog.text
    )


@pytest.mark.asyncio
async def test_text_generation_service_skips_unknown_reasoning_effort_even_without_emit_flag(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen-model",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="droid",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("droid-model",),
            ),
        ]
    )
    qwen = RecordingAdapter("qwen")
    droid = RecordingAdapter("droid")
    service = TextGenerationService(registry, {"qwen": qwen, "droid": droid})
    caplog.set_level(logging.WARNING, logger=TEXT_GENERATION_LOGGER)

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            candidates=(
                FeatureCandidateConfig(candidate="qwen/qwen-model", reasoning_effort="banana"),
                FeatureCandidateConfig(candidate="droid/droid-model", reasoning_effort="high"),
            ),
        )
    )

    assert result.provider == "droid"
    assert qwen.requests == []
    assert droid.requests[0].reasoning_effort == "high"
    assert "Unknown reasoning_effort 'banana'" in caplog.text


@pytest.mark.asyncio
async def test_text_generation_service_skips_provider_unsupported_reasoning_effort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="droid",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("claude-opus-4-7",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="grok",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("grok-4",),
            ),
        ]
    )
    droid = RecordingAdapter("droid")
    grok = RecordingAdapter("grok")
    service = TextGenerationService(registry, {"droid": droid, "grok": grok})
    caplog.set_level(logging.WARNING, logger=TEXT_GENERATION_LOGGER)

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            candidates=(
                FeatureCandidateConfig(
                    candidate="droid/claude-opus-4-7",
                    reasoning_effort="xhigh",
                ),
                FeatureCandidateConfig(candidate="grok/grok-4", reasoning_effort="high"),
            ),
        )
    )

    assert result.provider == "grok"
    assert droid.requests == []
    assert grok.requests[0].reasoning_effort == "high"
    assert "Unsupported reasoning_effort 'xhigh' for provider 'droid'" in caplog.text


@pytest.mark.asyncio
async def test_text_generation_service_raises_last_reasoning_effort_error_when_all_reject() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen-model",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="grok",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("grok-model",),
            ),
        ]
    )
    qwen = RecordingAdapter("qwen")
    grok = RecordingAdapter("grok")
    service = TextGenerationService(registry, {"qwen": qwen, "grok": grok})

    with pytest.raises(ValueError, match="Unknown reasoning_effort 'kumquat'"):
        await service.generate_result(
            TextGenerationRequest(
                prompt="summarize",
                candidates=(
                    FeatureCandidateConfig(
                        candidate="qwen/qwen-model",
                        reasoning_effort="banana",
                    ),
                    FeatureCandidateConfig(
                        candidate="grok/grok-model",
                        reasoning_effort="kumquat",
                    ),
                ),
            )
        )

    assert qwen.requests == []
    assert grok.requests == []


def test_coerce_text_result_applies_reasoning_effort_to_raw_string() -> None:
    result = _coerce_text_result("Generated text", applied_reasoning_effort="high")

    assert result.text == "Generated text"
    assert result.applied_reasoning_effort == "high"


def test_coerce_text_result_applies_reasoning_effort_to_text_result() -> None:
    result = _coerce_text_result(
        LLMTextResult(text="Generated text", usage={"total_tokens": 3}),
        applied_reasoning_effort="high",
    )

    assert result.text == "Generated text"
    assert result.usage == {"total_tokens": 3}
    assert result.applied_reasoning_effort == "high"


@pytest.mark.asyncio
async def test_text_generation_service_candidate_list_is_exhaustive_for_unavailable_override() -> (
    None
):
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=False,
                models=("haiku",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.4-mini",),
            ),
        ]
    )
    codex = RecordingAdapter("codex")
    service = TextGenerationService(registry, {"codex": codex})

    with pytest.raises(CapabilityUnavailableError, match="provider=claude"):
        await service.generate_result(
            TextGenerationRequest(
                prompt="summarize",
                profile="feature_low",
                candidates=("claude/haiku",),
                caller="session_summary",
            )
        )

    assert codex.requests == []


@pytest.mark.asyncio
async def test_text_generation_service_aggregates_all_unavailable_candidates() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                reason="Claude CLI is not installed.",
                models=("haiku",),
            ),
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "codex",
                adapter_style=AIAdapterStyle.DAEMON,
                reason="Codex app server is not available.",
                models=("gpt-5.4-mini",),
            ),
        ]
    )
    service = TextGenerationService(registry, {})

    with pytest.raises(CapabilityUnavailableError) as exc_info:
        await service.generate_result(
            TextGenerationRequest(
                prompt="summarize",
                profile="feature_low",
                candidates=("claude/haiku", "codex/gpt-5.4-mini"),
            )
        )

    error = exc_info.value
    assert error.provider is None
    assert error.model is None
    assert error.reason is not None
    assert error.reason.startswith("All text generation candidates unavailable:")
    assert "provider=claude" in error.reason
    assert "provider=codex" in error.reason


@pytest.mark.asyncio
async def test_text_generation_service_preserves_duplicate_candidate_errors() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                reason="Claude CLI is not installed.",
                models=("haiku", "sonnet"),
            ),
        ]
    )
    service = TextGenerationService(registry, {})

    with pytest.raises(CapabilityUnavailableError) as exc_info:
        await service.generate_result(
            TextGenerationRequest(
                prompt="summarize",
                profile="feature_low",
                candidates=("claude/haiku", "claude/sonnet"),
            )
        )

    error = exc_info.value
    assert error.reason is not None
    assert error.reason.count("provider=claude") == 2
    assert "claude/haiku" in error.reason
    assert "claude/sonnet" in error.reason


@pytest.mark.asyncio
async def test_text_generation_service_aggregates_all_unavailable_json_candidates() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                reason="Claude CLI is not installed.",
                models=("haiku",),
            ),
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "codex",
                adapter_style=AIAdapterStyle.DAEMON,
                reason="Codex app server is not available.",
                models=("gpt-5.4-mini",),
            ),
        ]
    )
    service = TextGenerationService(registry, {})

    with pytest.raises(CapabilityUnavailableError) as exc_info:
        await service.generate_json(
            TextGenerationRequest(
                prompt="summarize",
                profile="feature_low",
                candidates=("claude/haiku", "codex/gpt-5.4-mini"),
            )
        )

    error = exc_info.value
    assert error.provider is None
    assert error.model is None
    assert error.reason is not None
    assert error.reason.startswith("All JSON generation candidates unavailable:")
    assert "provider=claude" in error.reason
    assert "provider=codex" in error.reason


@pytest.mark.asyncio
async def test_text_generation_service_json_candidates_do_not_fallback_to_profile_defaults() -> (
    None
):
    from gobby.llm.claude import ClaudeSDKProviderFailure

    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("haiku",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.4-mini",),
            ),
        ]
    )
    claude = ProviderFailureAdapter(ClaudeSDKProviderFailure("provider degraded"))
    codex = JSONAdapter("codex")
    service = TextGenerationService(registry, {"claude": claude, "codex": codex})

    with pytest.raises(ClaudeSDKProviderFailure, match="provider degraded"):
        await service.generate_json(
            TextGenerationRequest(
                prompt="classify",
                profile="feature_low",
                candidates=("claude/haiku",),
                caller="session_summary",
            )
        )

    assert claude.requests[0].model == "haiku"
    assert codex.requests == []


@pytest.mark.asyncio
async def test_text_generation_service_json_propagates_provider_cancellation() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("haiku",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.4-mini",),
            ),
        ]
    )
    claude = ProviderFailureAdapter(LLMProviderCancellation("shutdown"))
    codex = JSONAdapter("codex")
    service = TextGenerationService(registry, {"claude": claude, "codex": codex})

    with pytest.raises(LLMProviderCancellation, match="shutdown"):
        await service.generate_json(
            TextGenerationRequest(
                prompt="classify",
                profile="feature_low",
                candidates=("claude/haiku", "codex/gpt-5.4-mini"),
            )
        )

    assert claude.requests[0].model == "haiku"
    assert codex.requests == []


@pytest.mark.asyncio
async def test_text_generation_service_skips_profile_fallback_for_non_recoverable_failure() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("haiku",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.3-codex-spark",),
            ),
        ]
    )
    claude = FailingAdapter("malformed model output")
    codex = RecordingAdapter("codex")
    service = TextGenerationService(registry, {"claude": claude, "codex": codex})

    with pytest.raises(RuntimeError, match="malformed model output"):
        await service.generate_result(
            TextGenerationRequest(
                prompt="summarize",
                profile="feature_low",
                candidates=("claude/haiku",),
                caller="session_summary",
            )
        )

    assert claude.requests[0].model == "haiku"
    assert codex.requests == []


@pytest.mark.asyncio
async def test_text_generation_service_normalizes_claude_family_candidate() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("haiku",),
            ),
        ]
    )
    claude = RecordingAdapter("claude")
    service = TextGenerationService(registry, {"claude": claude})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            candidates=("claude/claude-haiku-4-5",),
        )
    )

    assert result.text == "claude:summarize"
    assert result.provider == "claude"
    assert result.model == "haiku"
    assert claude.requests[0].model == "haiku"


@pytest.mark.asyncio
async def test_text_generation_service_uses_native_json_adapter() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-local",),
            )
        ]
    )
    adapter = JSONAdapter("local:lm-studio")
    service = TextGenerationService(registry, {"local:lm-studio": adapter})

    result = await service.generate_json(
        TextGenerationRequest(prompt="classify", provider="local:lm-studio", model="qwen-local")
    )

    assert result == {"provider": "local:lm-studio", "model": "qwen-local"}


@pytest.mark.asyncio
async def test_text_generation_service_parses_json_text_fallback() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.3-codex",),
            )
        ]
    )
    adapter = JSONTextAdapter("codex")
    service = TextGenerationService(registry, {"codex": adapter})

    result = await service.generate_json(
        TextGenerationRequest(prompt="classify", provider="codex", model="gpt-5.3-codex")
    )

    assert result == {"ok": True, "model": "gpt-5.3-codex"}
    assert adapter.requests[0].system_prompt is not None
    assert "valid JSON object" in adapter.requests[0].system_prompt


@pytest.mark.asyncio
async def test_text_generation_service_json_parse_failure_reports_raw_preview() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen-model",),
            )
        ]
    )
    adapter = EmptyTextAdapter("qwen")
    service = TextGenerationService(registry, {"qwen": adapter})

    with pytest.raises(ValueError) as exc_info:
        await service.generate_json(
            TextGenerationRequest(prompt="classify", provider="qwen", model="qwen-model")
        )

    message = str(exc_info.value)
    assert "Generated JSON parse failed" in message
    assert "raw_len=0" in message
    assert "raw_preview='<empty>'" in message


@pytest.mark.asyncio
async def test_text_generation_service_resolves_only_selected_adapter() -> None:
    providers = ("claude", "codex", "local:lm-studio", "grok", "qwen", "droid")
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider=provider,
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
            )
            for provider in providers
        ]
    )
    created: list[str] = []

    def factory(provider: str) -> RecordingAdapter:
        created.append(provider)
        return RecordingAdapter(provider)

    service = TextGenerationService(
        registry,
        adapter_factories={
            provider: (lambda provider=provider: factory(provider)) for provider in providers
        },
    )

    response = await service.generate(
        TextGenerationRequest(prompt="summarize", provider="codex", model="codex-model")
    )
    second_response = await service.generate(
        TextGenerationRequest(
            prompt="summarize again",
            provider="codex",
            model="codex-model",
        )
    )

    assert response == "codex:summarize"
    assert second_response == "codex:summarize again"
    assert created == ["codex"]


@pytest.mark.asyncio
async def test_text_generation_service_rejects_none_factory_result() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
            )
        ]
    )
    calls = 0

    def none_factory() -> Any:
        nonlocal calls
        calls += 1
        return None

    service = TextGenerationService(registry, adapter_factories={"codex": none_factory})

    for _ in range(2):
        with pytest.raises(RuntimeError, match="returned None"):
            await service.generate(
                TextGenerationRequest(prompt="summarize", provider="codex", model="codex-model")
            )

    assert calls == 2


def test_build_daemon_text_generation_service_defers_adapter_instantiation() -> None:
    providers = ("claude", "codex", "local:lm-studio", "agy", "grok", "qwen", "droid")
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider=provider,
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
            )
            for provider in providers
        ]
    )

    service = build_daemon_text_generation_service(
        DaemonConfig(
            ai={
                "generation": {
                    "local": {
                        "endpoints": {
                            "lm-studio": {
                                "api_base": "http://localhost:1234/v1",
                                "model": "llama",
                            }
                        }
                    }
                }
            }
        ),
        registry=registry,
    )

    assert service.registry is registry
    assert {
        binding.provider
        for binding in service.registry.bindings_for(
            AICapability.TEXT_GENERATE,
            include_unavailable=False,
        )
    } == set(providers)


def test_daemon_text_generation_builder_maps_feature_providers_to_one_shot_adapters() -> None:
    config = DaemonConfig(
        ai={
            "generation": {
                "local": {
                    "endpoints": {
                        "ollama": {
                            "api_base": "http://localhost:11434/v1",
                            "model": "llama3.2",
                        }
                    }
                }
            }
        }
    )
    factories = _daemon_text_generation_adapter_factories(config)

    codex_adapter = factories["codex"]()
    agy_adapter = factories["agy"]()
    grok_adapter = factories["grok"]()
    qwen_adapter = factories["qwen"]()

    assert isinstance(codex_adapter, CodexCLITextGenerateAdapter)
    assert isinstance(agy_adapter, AgyCLITextGenerateAdapter)
    assert isinstance(grok_adapter, text_generation_adapters._GrokCLITextGenerateAdapter)
    assert isinstance(qwen_adapter, text_generation_adapters._QwenCLITextGenerateAdapter)
    qwen_command = qwen_adapter.build_command(
        TextGenerationRequest(prompt="x", model="stale-model")
    )
    assert "--openai-base-url" in qwen_command
    assert "http://localhost:11434/v1" in qwen_command
    assert qwen_command[qwen_command.index("--model") + 1] == "llama3.2"


class FakeNativeTextProvider:
    last_instance: ClassVar[FakeNativeTextProvider | None] = None

    def __init__(self, config: DaemonConfig, endpoint_name: str | None = None) -> None:
        self.config = config
        self.endpoint_name = endpoint_name
        self.text_calls: list[
            tuple[str, str | None, str | None, int | None, str | None, str | None]
        ] = []
        self.json_calls: list[tuple[str, str | None, str | None, str | None, str | None]] = []
        self.__class__.last_instance = self

    async def generate_text_result(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        reasoning_effort: str | None = None,
        caller: str | None = None,
    ) -> LLMTextResult:
        self.text_calls.append((prompt, system_prompt, model, max_tokens, reasoning_effort, caller))
        return LLMTextResult(
            text=f"{system_prompt}:{prompt}:{model}:{max_tokens}:{reasoning_effort}:{caller}",
            usage={"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
        )

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        *,
        reasoning_effort: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        self.json_calls.append((prompt, system_prompt, model, reasoning_effort, caller))
        return {
            "prompt": prompt,
            "system_prompt": system_prompt,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "caller": caller,
        }


@pytest.mark.asyncio
async def test_claude_text_generate_adapter_forwards_usage_and_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeNativeTextProvider.last_instance = None
    monkeypatch.setattr("gobby.llm.claude.ClaudeLLMProvider", FakeNativeTextProvider)
    config = DaemonConfig()
    adapter = ClaudeTextGenerateAdapter(config)

    response = await adapter.generate(
        TextGenerationRequest(
            prompt="hello",
            system_prompt="system",
            model="model-a",
            max_tokens=42,
            reasoning_effort="xhigh",
            caller="test",
        )
    )

    provider = FakeNativeTextProvider.last_instance
    assert provider is not None
    assert provider.config is config
    assert provider.text_calls == [("hello", "system", "model-a", 42, "xhigh", "test")]
    assert response.text == "system:hello:model-a:42:xhigh:test"
    assert response.usage == {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}


@pytest.mark.asyncio
async def test_local_text_generate_adapter_forwards_json_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeNativeTextProvider.last_instance = None
    monkeypatch.setattr("gobby.llm.local.LocalLLMProvider", FakeNativeTextProvider)
    config = DaemonConfig()
    adapter = LocalTextGenerateAdapter(config, "lm-studio")

    response = await adapter.generate_json(
        TextGenerationRequest(
            prompt="json please",
            system_prompt="system",
            model="model-b",
            reasoning_effort="high",
            caller="test",
        )
    )

    provider = FakeNativeTextProvider.last_instance
    assert provider is not None
    assert provider.config is config
    assert provider.endpoint_name == "lm-studio"
    assert provider.json_calls == [("json please", "system", "model-b", "high", "test")]
    assert response == {
        "prompt": "json please",
        "system_prompt": "system",
        "model": "model-b",
        "reasoning_effort": "high",
        "caller": "test",
    }


class FakeACPClient:
    def __init__(self, events: list[StreamEvent] | None = None) -> None:
        self.started: dict[str, object] | None = None
        self.sent: list[dict[str, object]] = []
        self.stopped = False
        self.events = events or [
            StreamEvent(event_type="content_delta", data={"content": "hello "}),
            StreamEvent(event_type="content_delta", data={"content": "world"}),
            StreamEvent(event_type="result", data={"content": "ignored fallback"}),
        ]

    async def start(self, **kwargs: object) -> None:
        self.started = kwargs

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, message: str, **kwargs: object) -> AsyncIterator[StreamEvent]:
        self.sent.append({"message": message, **kwargs})
        for event in self.events:
            yield event


class HangingACPClient(FakeACPClient):
    async def send(self, message: str, **kwargs: object) -> AsyncIterator[StreamEvent]:
        self.sent.append({"message": message, **kwargs})
        await asyncio.Event().wait()
        yield StreamEvent(event_type="content_delta", data={"content": "unreachable"})


class FakeCodexAppServerClient:
    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        *,
        connected: bool = False,
        thread_ids: list[str] | None = None,
    ) -> None:
        self.started = False
        self.stopped = False
        self.start_calls = 0
        self.stop_calls = 0
        self.connected = connected
        self.thread_kwargs: dict[str, object] | None = None
        self.thread_kwargs_list: list[dict[str, object]] = []
        self.turn_kwargs: dict[str, object] | None = None
        self.turn_kwargs_list: list[dict[str, object]] = []
        self.archived_thread_ids: list[str] = []
        self.thread_ids = thread_ids or ["thread-1"]
        self.events = events or [
            {"type": "item/agentMessage/delta", "delta": "hello "},
            {
                "type": "item/completed",
                "item": {"content": [{"text": "ignored fallback"}]},
            },
            {"type": "item/agentMessage/delta", "delta": "world"},
        ]

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def start(self) -> None:
        self.start_calls += 1
        self.started = True
        self.connected = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.stopped = True
        self.connected = False

    async def start_thread(
        self,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        ephemeral: bool = False,
    ) -> SimpleNamespace:
        self.thread_kwargs = {
            "cwd": cwd,
            "model": model,
            "approval_policy": approval_policy,
            "sandbox": sandbox,
            "ephemeral": ephemeral,
        }
        self.thread_kwargs_list.append(self.thread_kwargs)
        index = len(self.thread_kwargs_list) - 1
        thread_id = (
            self.thread_ids[index] if index < len(self.thread_ids) else f"thread-{index + 1}"
        )
        return SimpleNamespace(id=thread_id)

    async def run_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        **config_overrides: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.turn_kwargs = {
            "thread_id": thread_id,
            "prompt": prompt,
            "images": images,
            **config_overrides,
        }
        self.turn_kwargs_list.append(self.turn_kwargs)
        for event in self.events:
            yield event

    async def archive_thread(self, thread_id: str) -> None:
        self.archived_thread_ids.append(thread_id)


class HangingCodexAppServerClient(FakeCodexAppServerClient):
    async def run_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        **config_overrides: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.turn_kwargs = {
            "thread_id": thread_id,
            "prompt": prompt,
            "images": images,
            **config_overrides,
        }
        self.turn_kwargs_list.append(self.turn_kwargs)
        await asyncio.Event().wait()
        yield {"type": "item/agentMessage/delta", "delta": "unreachable"}


class DisconnectingCodexAppServerClient(FakeCodexAppServerClient):
    async def run_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        **config_overrides: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.turn_kwargs = {
            "thread_id": thread_id,
            "prompt": prompt,
            "images": images,
            **config_overrides,
        }
        self.turn_kwargs_list.append(self.turn_kwargs)
        self.connected = False
        raise ConnectionError("codex app-server disconnected")
        yield {"type": "item/agentMessage/delta", "delta": "unreachable"}


@pytest.mark.asyncio
async def test_codex_cli_text_generate_adapter_runs_one_shot_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_run_cli(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("final answer\n", encoding="utf-8")
        calls.append(
            {
                "provider_name": provider_name,
                "command": command,
                "neutral_cwd": neutral_cwd,
                "timeout_seconds": timeout_seconds,
                "env_overrides": env_overrides,
            }
        )
        return "stdout is ignored"

    monkeypatch.setattr(text_generation_adapters, "_run_cli_text_generation_command", fake_run_cli)
    adapter = CodexCLITextGenerateAdapter(
        command_path="/bin/codex",
        timeout_seconds=12.0,
        env={"EXTRA": "1"},
    )

    response = await adapter.generate(
        TextGenerationRequest(
            provider="codex",
            prompt="user prompt",
            system_prompt="system prompt",
            model="gpt-5.4-mini",
            reasoning_effort="xhigh",
            cwd="/tmp/project",
        )
    )

    assert response == "final answer"
    assert len(calls) == 1
    call = calls[0]
    command = call["command"]
    assert call["provider_name"] == "Codex"
    assert call["timeout_seconds"] == 12.0
    assert call["env_overrides"] == {"EXTRA": "1"}
    # One-shot generation runs in a neutral temp dir, never the request's project cwd.
    neutral_cwd = call["neutral_cwd"]
    assert isinstance(neutral_cwd, Path)
    assert neutral_cwd != Path("/tmp/project")
    # Codex output file lives inside the neutral cwd so its lifetime matches the call.
    assert Path(command[command.index("--output-last-message") + 1]).parent == neutral_cwd
    assert command[:10] == [
        "/bin/codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
    ]
    # Codex aborts outside a Git repo; the neutral temp cwd is not one.
    assert "--skip-git-repo-check" in command
    assert "--output-last-message" in command
    assert command[command.index("--model") + 1] == "gpt-5.4-mini"
    assert command[command.index("-c") + 1] == 'model_reasoning_effort="xhigh"'
    # request.cwd must NOT leak into the command as --cd.
    assert "--cd" not in command
    assert command[-1] == f"system prompt\n\n{ONE_SHOT_DIRECTIVE}\n\nuser prompt"


def test_cli_text_generate_adapters_treat_auto_reasoning_effort_as_unset() -> None:
    codex = CodexCLITextGenerateAdapter(command_path="/bin/codex")
    droid = DroidCLITextGenerateAdapter(command_path="/bin/droid")
    grok = text_generation_adapters._GrokCLITextGenerateAdapter(command_path="/bin/grok")
    request = TextGenerationRequest(prompt="prompt", reasoning_effort="auto")

    codex_command = codex.build_command(request, output_path=Path("/tmp/last-message.txt"))
    droid_command = droid.build_command(request)
    grok_command = grok.build_command(request, leader_socket=Path("/tmp/leader.sock"))

    assert "model_reasoning_effort" not in " ".join(codex_command)
    assert "--reasoning-effort" not in droid_command
    assert "--reasoning-effort" not in grok_command


def test_emit_nothing_cli_text_generate_adapters_ignore_reasoning_effort() -> None:
    qwen = text_generation_adapters._QwenCLITextGenerateAdapter(command_path="/bin/qwen")
    request = TextGenerationRequest(
        prompt="prompt",
        model="model-a",
        reasoning_effort="high",
    )

    qwen_command = qwen.build_command(request)

    assert "--reasoning-effort" not in qwen_command
    assert "model_reasoning_effort" not in " ".join(qwen_command)


@pytest.mark.asyncio
async def test_daemon_codex_text_generate_adapter_uses_configured_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_timeouts: list[float] = []

    async def fake_run_cli(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        recorded_timeouts.append(timeout_seconds)
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("ok", encoding="utf-8")
        return "ignored"

    monkeypatch.setattr(text_generation_adapters, "_run_cli_text_generation_command", fake_run_cli)
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
            )
        ]
    )
    service = build_daemon_text_generation_service(
        DaemonConfig(ai={"generation": {"timeout_seconds": 0.01}}),
        registry=registry,
    )

    result = await service.generate_result(
        TextGenerationRequest(
            provider="codex",
            model="gpt-5",
            prompt="complete",
        )
    )

    assert result.text == "ok"
    assert recorded_timeouts == [0.01]


@pytest.mark.asyncio
async def test_text_generation_service_falls_back_when_candidate_errors() -> None:
    class FailingAdapter:
        async def generate(self, request: TextGenerationRequest) -> str:
            raise RuntimeError("provider unavailable")

    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen-model",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("haiku",),
            ),
        ]
    )
    claude = RecordingAdapter("claude")
    service = TextGenerationService(
        registry,
        {
            "qwen": FailingAdapter(),
            "claude": claude,
        },
    )

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            profile="feature_low",
            candidates=("qwen/qwen-model", "claude/haiku"),
        )
    )

    assert result.text == "claude:summarize"


@pytest.mark.asyncio
async def test_json_text_generation_composes_json_instruction() -> None:
    class RecordingJSONTextAdapter:
        def __init__(self) -> None:
            self.requests: list[TextGenerationRequest] = []

        async def generate(self, request: TextGenerationRequest) -> str:
            self.requests.append(request)
            return '{"ok": true}'

    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
                models=("qwen-model",),
            )
        ]
    )
    adapter = RecordingJSONTextAdapter()
    service = TextGenerationService(registry, {"qwen": adapter})

    result = await service.generate_json(
        TextGenerationRequest(
            prompt="classify",
            system_prompt="caller prompt",
            provider="qwen",
            model="qwen-model",
        )
    )

    assert result == {"ok": True}
    assert adapter.requests[0].system_prompt == (
        "caller prompt\n\nRespond with a single valid JSON object. Do not include markdown."
    )


def _two_candidate_registry(slow_provider: str, good_provider: str) -> AICapabilityRegistry:
    return AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider=slow_provider,
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("slow-model",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider=good_provider,
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("good-model",),
            ),
        ]
    )


def _registry_for_text_generation(
    *bindings: tuple[str, AIAdapterStyle, str],
) -> AICapabilityRegistry:
    return AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider=provider,
                adapter_style=adapter_style,
                available=True,
                models=(model,),
            )
            for provider, adapter_style, model in bindings
        ]
    )


@pytest.mark.asyncio
async def test_spawn_cold_same_provider_calls_respect_global_concurrency_cap() -> None:
    prompts = {f"hold-{index}" for index in range(5)}
    state = GateProbeState(expected_started=3)
    service = TextGenerationService(
        _registry_for_text_generation(("qwen", AIAdapterStyle.CLI, "qwen-model")),
        {"qwen": GateProbeAdapter(state, wait_prompts=prompts)},
        spawn_cold_max_concurrency=3,
    )
    tasks = [
        asyncio.create_task(
            service.generate_result(
                TextGenerationRequest(
                    provider="qwen",
                    model="qwen-model",
                    prompt=f"hold-{index}",
                )
            )
        )
        for index in range(5)
    ]

    try:
        await asyncio.wait_for(state.started_event.wait(), timeout=1)
        assert len(state.started) == 3
        assert state.max_active == 3
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(state.over_expected_event.wait(), timeout=0.02)
    finally:
        state.release.set()

    results = await asyncio.gather(*tasks)

    assert len(results) == 5
    assert len(state.started) == 5
    assert state.max_active == 3


@pytest.mark.asyncio
async def test_spawn_cold_mixed_provider_calls_share_global_concurrency_cap() -> None:
    requests = [
        ("qwen", "qwen-model", "hold-qwen-1"),
        ("codex", "gpt-5", "hold-codex-1"),
        ("qwen", "qwen-model", "hold-qwen-2"),
        ("codex", "gpt-5", "hold-codex-2"),
        ("qwen", "qwen-model", "hold-qwen-3"),
    ]
    state = GateProbeState(expected_started=3)
    adapter = GateProbeAdapter(
        state,
        wait_prompts={prompt for _, _, prompt in requests},
    )
    service = TextGenerationService(
        _registry_for_text_generation(
            ("qwen", AIAdapterStyle.CLI, "qwen-model"),
            ("codex", AIAdapterStyle.DAEMON, "gpt-5"),
        ),
        {"qwen": adapter, "codex": adapter},
        spawn_cold_max_concurrency=3,
    )
    tasks = [
        asyncio.create_task(
            service.generate_result(
                TextGenerationRequest(provider=provider, model=model, prompt=prompt)
            )
        )
        for provider, model, prompt in requests
    ]

    try:
        await asyncio.wait_for(state.started_event.wait(), timeout=1)
        assert len(state.started) == 3
        assert set(state.started) == {"qwen", "codex"}
        assert state.max_active == 3
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(state.over_expected_event.wait(), timeout=0.02)
    finally:
        state.release.set()

    await asyncio.gather(*tasks)

    assert len(state.started) == 5
    assert state.max_active == 3


@pytest.mark.asyncio
async def test_fast_generation_lanes_bypass_spawn_cold_gate() -> None:
    prompts = {f"hold-{index}" for index in range(3)}
    state = GateProbeState(expected_started=3)
    service = TextGenerationService(
        _registry_for_text_generation(
            ("local:lm-studio", AIAdapterStyle.OPENAI_COMPATIBLE, "local-model")
        ),
        {"local:lm-studio": GateProbeAdapter(state, wait_prompts=prompts)},
        spawn_cold_max_concurrency=1,
    )
    tasks = [
        asyncio.create_task(
            service.generate_result(
                TextGenerationRequest(
                    provider="local:lm-studio",
                    model="local-model",
                    prompt=f"hold-{index}",
                )
            )
        )
        for index in range(3)
    ]

    try:
        await asyncio.wait_for(state.started_event.wait(), timeout=1)
        assert len(state.started) == 3
        assert state.max_active == 3
    finally:
        state.release.set()

    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_spawn_cold_queue_wait_does_not_consume_candidate_timeout() -> None:
    state = GateProbeState(expected_started=1)
    service = TextGenerationService(
        _registry_for_text_generation(("qwen", AIAdapterStyle.CLI, "qwen-model")),
        {"qwen": GateProbeAdapter(state, delays={"slow": 30.0, "fast": 0.02})},
        cli_candidate_timeout_seconds=0.05,
        spawn_cold_max_concurrency=1,
    )
    slow_task = asyncio.create_task(
        service.generate_result(
            TextGenerationRequest(provider="qwen", model="qwen-model", prompt="slow")
        )
    )
    await asyncio.wait_for(state.started_event.wait(), timeout=1)

    started_waiting_at = asyncio.get_running_loop().time()
    fast_task = asyncio.create_task(
        service.generate_result(
            TextGenerationRequest(provider="qwen", model="qwen-model", prompt="fast")
        )
    )

    with pytest.raises(RuntimeError, match="candidate timed out after 0.05s"):
        await slow_task
    result = await asyncio.wait_for(fast_task, timeout=1)

    assert asyncio.get_running_loop().time() - started_waiting_at >= 0.05
    assert result.text == "qwen:fast"


@pytest.mark.asyncio
async def test_spawn_cold_gate_releases_slot_after_provider_error() -> None:
    state = GateProbeState(expected_started=1)
    service = TextGenerationService(
        _registry_for_text_generation(("qwen", AIAdapterStyle.CLI, "qwen-model")),
        {"qwen": GateProbeAdapter(state, failures={"error"})},
        spawn_cold_max_concurrency=1,
    )

    with pytest.raises(RuntimeError, match="boom"):
        await service.generate_result(
            TextGenerationRequest(provider="qwen", model="qwen-model", prompt="error")
        )
    result = await asyncio.wait_for(
        service.generate_result(
            TextGenerationRequest(provider="qwen", model="qwen-model", prompt="success")
        ),
        timeout=1,
    )

    assert result.text == "qwen:success"


@pytest.mark.asyncio
async def test_spawn_cold_gate_releases_slot_after_cancellation() -> None:
    state = GateProbeState(expected_started=1)
    service = TextGenerationService(
        _registry_for_text_generation(("qwen", AIAdapterStyle.CLI, "qwen-model")),
        {"qwen": GateProbeAdapter(state, wait_prompts={"wait"})},
        spawn_cold_max_concurrency=1,
    )
    cancelled_task = asyncio.create_task(
        service.generate_result(
            TextGenerationRequest(provider="qwen", model="qwen-model", prompt="wait")
        )
    )
    await asyncio.wait_for(state.started_event.wait(), timeout=1)

    cancelled_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task
    result = await asyncio.wait_for(
        service.generate_result(
            TextGenerationRequest(provider="qwen", model="qwen-model", prompt="success")
        ),
        timeout=1,
    )

    assert result.text == "qwen:success"


@pytest.mark.asyncio
async def test_text_generation_service_times_out_slow_candidate_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = TextGenerationService(
        _two_candidate_registry("local:slow", "local:good"),
        {"local:slow": SlowAdapter(), "local:good": RecordingAdapter("local:good")},
        candidate_timeout_seconds=0.01,
    )
    caplog.set_level(logging.DEBUG, logger=TEXT_GENERATION_LOGGER)

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            candidates=("local:slow/slow-model", "local:good/good-model"),
        )
    )

    assert result.provider == "local:good"
    records = [record for record in caplog.records if record.getMessage() == "feature_llm_call"]
    assert [record.levelno for record in records] == [logging.DEBUG, logging.DEBUG]
    assert records[0].success is False
    assert "candidate timed out after 0.01s" in records[0].error


@pytest.mark.asyncio
async def test_text_generation_service_times_out_slow_json_candidate_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = TextGenerationService(
        _two_candidate_registry("local:slow", "local:good"),
        {"local:slow": SlowAdapter(), "local:good": JSONAdapter("local:good")},
        candidate_timeout_seconds=0.01,
    )
    caplog.set_level(logging.DEBUG, logger=TEXT_GENERATION_LOGGER)

    result = await service.generate_json(
        TextGenerationRequest(
            prompt="classify",
            candidates=("local:slow/slow-model", "local:good/good-model"),
        )
    )

    assert result == {"provider": "local:good", "model": "good-model"}
    records = [record for record in caplog.records if record.getMessage() == "feature_llm_call"]
    assert records[0].success is False
    assert "candidate timed out after 0.01s" in records[0].error


@pytest.mark.asyncio
async def test_text_generation_service_terminal_candidate_timeout_raises() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:slow",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("slow-model",),
            )
        ]
    )
    service = TextGenerationService(
        registry,
        {"local:slow": SlowAdapter()},
        candidate_timeout_seconds=0.01,
    )

    with pytest.raises(RuntimeError, match="candidate timed out after 0.01s"):
        await service.generate_result(
            TextGenerationRequest(prompt="summarize", provider="local:slow", model="slow-model")
        )


@pytest.mark.asyncio
async def test_text_generation_service_no_candidate_timeout_when_unset() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="local:slow",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("slow-model",),
            )
        ]
    )
    service = TextGenerationService(registry, {"local:slow": SlowAdapter(delay=0.05)})

    result = await service.generate_result(
        TextGenerationRequest(prompt="summarize", provider="local:slow", model="slow-model")
    )

    assert result.text == "slow text"


@pytest.mark.asyncio
async def test_build_daemon_text_generation_service_plumbs_candidate_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slow_adapter = SlowAdapter()
    monkeypatch.setattr(
        text_generation_adapters,
        "_QwenCLITextGenerateAdapter",
        lambda **_kwargs: slow_adapter,
    )
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="qwen",
                adapter_style=AIAdapterStyle.CLI,
                available=True,
            )
        ]
    )
    # The qwen binding is a spawn-cold CLI lane, so it is bounded by
    # cli_candidate_timeout_seconds (not the fast-lane candidate_timeout_seconds).
    service = build_daemon_text_generation_service(
        DaemonConfig(
            ai={
                "generation": {
                    "cli_candidate_timeout_seconds": 0.01,
                    "spawn_cold_max_concurrency": 1,
                }
            }
        ),
        registry=registry,
    )

    assert service._spawn_cold_max_concurrency == 1
    with pytest.raises(RuntimeError, match="candidate timed out after 0.01s"):
        await service.generate(
            TextGenerationRequest(provider="qwen", model="qwen-model", prompt="never completes")
        )


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int | None = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = 4242
        self.terminated = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


class HangingProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__(b"", returncode=None)
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(10)
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        super().kill()


@pytest.mark.asyncio
async def test_run_cli_text_generation_command_cleans_up_process_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    class ObservableHangingProcess(HangingProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.get_running_loop().create_future()
            raise AssertionError("unreachable")

    process = ObservableHangingProcess()

    async def fake_create_subprocess_exec(
        *command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        assert start_new_session is True
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    task = asyncio.create_task(
        text_generation_adapters._run_cli_text_generation_command(
            "Qwen",
            ("/usr/local/bin/qwen", "--prompt", "slow"),
            neutral_cwd=Path("/tmp"),
            timeout_seconds=30,
            env_overrides={},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated is True
    assert process.killed is False


@pytest.mark.asyncio
async def test_run_cli_text_generation_command_signals_process_group_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    class ObservableHangingProcess(HangingProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.get_running_loop().create_future()
            raise AssertionError("unreachable")

    process = ObservableHangingProcess()
    signals: list[tuple[int, object]] = []

    async def fake_create_subprocess_exec(
        *command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        assert start_new_session is True
        return process

    def fake_signal_process_group(process_arg: FakeProcess, signal_arg: object) -> bool:
        signals.append((process_arg.pid, signal_arg))
        process_arg.returncode = -15
        return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        text_generation_adapters,
        "_signal_cli_process_group",
        fake_signal_process_group,
    )
    task = asyncio.create_task(
        text_generation_adapters._run_cli_text_generation_command(
            "Qwen",
            ("/usr/local/bin/qwen", "--model", "slow"),
            neutral_cwd=Path("/tmp"),
            timeout_seconds=30,
            env_overrides={},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert signals == [(process.pid, text_generation_adapters.signal.SIGTERM)]
    assert process.terminated is False
    assert process.killed is False


@pytest.mark.asyncio
async def test_text_generation_service_cleans_up_timed_out_cli_candidate_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CLIAdapter:
        async def generate(self, request: TextGenerationRequest) -> str:
            return await text_generation_adapters._run_cli_text_generation_command(
                "Slow",
                ("/usr/local/bin/slow", "--prompt", request.prompt),
                neutral_cwd=Path("/tmp"),
                timeout_seconds=30,
                env_overrides={},
            )

    process = HangingProcess()

    async def fake_create_subprocess_exec(
        *_command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        assert start_new_session is True
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(text_generation_adapters, "_signal_cli_process_group", lambda *_args: False)
    registry = _two_candidate_registry("local:slow-cli", "local:good")
    service = TextGenerationService(
        registry,
        {"local:slow-cli": CLIAdapter(), "local:good": RecordingAdapter("local:good")},
        candidate_timeout_seconds=0.01,
    )

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            candidates=("local:slow-cli/slow-model", "local:good/good-model"),
        )
    )

    assert result.provider == "local:good"
    assert process.terminated is True
    assert process.killed is False


def _assert_droid_isolated_env(env: dict[str, str]) -> Path:
    temp_home = Path(env["HOME"])
    # Droid home/state lives under the shared neutral textgen root (cwd / "home").
    assert temp_home.name == "home"
    assert temp_home.parent.name.startswith("gobby-textgen-")
    assert Path(env["XDG_CONFIG_HOME"]) == temp_home / ".config"
    assert Path(env["XDG_DATA_HOME"]) == temp_home / ".local" / "share"
    assert Path(env["XDG_STATE_HOME"]) == temp_home / ".local" / "state"
    assert Path(env["XDG_CACHE_HOME"]) == temp_home / ".cache"
    assert env["GOBBY_HOOKS_DISABLED"] == "1"
    return temp_home


@pytest.mark.asyncio
async def test_qwen_cli_text_generate_adapter_disables_recording_and_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    cwds: list[str | None] = []
    envs: list[dict[str, str]] = []

    async def fake_create_subprocess_exec(
        *command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        commands.append(command)
        cwds.append(cwd)
        envs.append(env)
        return FakeProcess(b"qwen text\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = text_generation_adapters._QwenCLITextGenerateAdapter(
        command_path="/usr/local/bin/qwen"
    )

    response = await adapter.generate(
        TextGenerationRequest(
            prompt="explain",
            system_prompt="system",
            model="qwen3-coder",
            cwd="/tmp/project",
        )
    )

    assert response == "qwen text"
    assert commands == [
        (
            "/usr/local/bin/qwen",
            "--bare",
            "--chat-recording=false",
            "--max-tool-calls",
            "0",
            "--max-session-turns",
            "1",
            "--output-format",
            "text",
            "--model",
            "qwen3-coder",
            f"system\n\n{ONE_SHOT_DIRECTIVE}\n\nexplain",
        )
    ]
    assert "--resume" not in commands[0]
    assert "--continue" not in commands[0]
    assert "--session-id" not in commands[0]
    # One-shot generation runs in a neutral temp dir, never the request's project cwd.
    assert cwds[0] != "/tmp/project"
    assert cwds[0] is not None and "gobby-textgen-" in cwds[0]
    assert envs[0]["GOBBY_HOOKS_DISABLED"] == "1"


@pytest.mark.asyncio
async def test_qwen_cli_text_generate_adapter_uses_configured_openai_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    envs: list[dict[str, str]] = []

    async def fake_create_subprocess_exec(
        *command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        commands.append(command)
        envs.append(env)
        return FakeProcess(b"qwen text\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = text_generation_adapters._QwenCLITextGenerateAdapter(
        command_path="/usr/local/bin/qwen",
        openai_endpoints={
            "ollama": SimpleNamespace(
                api_base="http://localhost:11434/v1",
                model="llama3.2",
                api_key=None,
            )
        },
    )

    response = await adapter.generate(TextGenerationRequest(prompt="explain", model="qwen3-coder"))

    assert response == "qwen text"
    assert commands == [
        (
            "/usr/local/bin/qwen",
            "--bare",
            "--chat-recording=false",
            "--max-tool-calls",
            "0",
            "--max-session-turns",
            "1",
            "--output-format",
            "text",
            "--auth-type",
            "openai",
            "--openai-base-url",
            "http://localhost:11434/v1",
            "--model",
            "llama3.2",
            f"{ONE_SHOT_DIRECTIVE}\n\nexplain",
        )
    ]
    assert "qwen3-coder" not in commands[0]
    assert "--openai-api-key" not in commands[0]
    assert envs[0]["OPENAI_API_KEY"] == "not-needed"
    assert envs[0]["OPENAI_BASE_URL"] == "http://localhost:11434/v1"
    assert envs[0]["OPENAI_MODEL"] == "llama3.2"


@pytest.mark.asyncio
async def test_agy_cli_text_generate_adapter_uses_hardened_print_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    cwds: list[str | None] = []
    envs: list[dict[str, str]] = []

    async def fake_create_subprocess_exec(
        *command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        assert start_new_session is True
        commands.append(command)
        cwds.append(cwd)
        envs.append(env)
        return FakeProcess(b"agy text\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = AgyCLITextGenerateAdapter(
        command_path="/usr/local/bin/agy",
        timeout_seconds=12.5,
        env={"EXTRA": "1"},
    )

    response = await adapter.generate(
        TextGenerationRequest(
            prompt="explain",
            system_prompt="system",
            model="gemini-3.5-flash-low",
            cwd="/tmp/project",
        )
    )

    assert response == "agy text"
    assert commands == [
        (
            "/usr/local/bin/agy",
            "--sandbox",
            "--print-timeout",
            "12.5s",
            "--model",
            "Gemini 3.5 Flash (Low)",
            "--print",
            f"system\n\n{ONE_SHOT_DIRECTIVE}\n\nexplain",
        )
    ]
    assert {"--continue", "--conversation", "--prompt-interactive"}.isdisjoint(commands[0])
    assert cwds[0] != "/tmp/project"
    assert cwds[0] is not None and "gobby-textgen-" in cwds[0]
    assert envs[0]["EXTRA"] == "1"
    assert envs[0]["GOBBY_HOOKS_DISABLED"] == "1"


def test_agy_cli_text_generate_adapter_omits_model_when_not_requested() -> None:
    adapter = AgyCLITextGenerateAdapter(command_path="/usr/local/bin/agy", timeout_seconds=90.0)

    command = adapter.build_command(TextGenerationRequest(prompt="explain"))

    assert command == [
        "/usr/local/bin/agy",
        "--sandbox",
        "--print-timeout",
        "90s",
        "--print",
        "explain",
    ]


def test_agy_cli_text_generate_adapter_rejects_unmapped_explicit_model() -> None:
    adapter = AgyCLITextGenerateAdapter(command_path="/usr/local/bin/agy")

    with pytest.raises(ValueError, match="Unsupported AGY model"):
        adapter.build_command(TextGenerationRequest(prompt="explain", model="not-real"))


@pytest.mark.parametrize(
    "stdout_bytes",
    [
        b"",
        b"Error: timed out waiting for response\n",
    ],
)
@pytest.mark.asyncio
async def test_agy_cli_text_generate_adapter_rejects_empty_or_error_stdout(
    monkeypatch: pytest.MonkeyPatch,
    stdout_bytes: bytes,
) -> None:
    async def fake_create_subprocess_exec(
        *command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        return FakeProcess(stdout_bytes)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = AgyCLITextGenerateAdapter(command_path="/usr/local/bin/agy")

    with pytest.raises(RuntimeError):
        await adapter.generate(
            TextGenerationRequest(prompt="explain", model="gemini-3.5-flash-low")
        )


@pytest.mark.asyncio
async def test_agy_cli_text_generate_adapter_generate_json_uses_directive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    async def fake_run_cli(
        provider_name: str,
        command: list[str],
        *,
        neutral_cwd: Path,
        timeout_seconds: float,
        env_overrides: dict[str, str],
    ) -> str:
        commands.append(command)
        assert provider_name == "AGY"
        assert timeout_seconds == 600.0
        assert neutral_cwd.name.startswith("gobby-textgen-")
        assert env_overrides == {}
        return '{"ok": true}'

    monkeypatch.setattr(text_generation_adapters, "_run_cli_text_generation_command", fake_run_cli)
    adapter = AgyCLITextGenerateAdapter(command_path="/usr/local/bin/agy")

    result = await adapter.generate_json(
        TextGenerationRequest(prompt="return json", model="gemini-3.5-flash-low")
    )

    assert result == {"ok": True}
    prompt = commands[0][-1]
    assert "Respond with a single valid JSON object" in prompt
    assert ONE_SHOT_DIRECTIVE in prompt


@pytest.mark.asyncio
async def test_grok_cli_text_generate_adapter_uses_non_session_headless_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []
    leader_socket_parent_exists: list[bool] = []
    cwds: list[str | None] = []

    async def fake_create_subprocess_exec(
        *command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        commands.append(command)
        cwds.append(cwd)
        leader_socket = Path(command[command.index("--leader-socket") + 1])
        leader_socket_parent_exists.append(leader_socket.parent.exists())
        return FakeProcess(b"grok text\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = text_generation_adapters._GrokCLITextGenerateAdapter(command_path="/usr/bin/grok")

    response = await adapter.generate(
        TextGenerationRequest(
            prompt="explain",
            system_prompt="system",
            model="grok-4",
            reasoning_effort="high",
            cwd="/tmp/project",
        )
    )

    assert response == "grok text"
    command = commands[0]
    assert command[:18] == (
        "/usr/bin/grok",
        "--output-format",
        "plain",
        "--permission-mode",
        "plan",
        "--max-turns",
        "1",
        "--no-memory",
        "--no-subagents",
        "--disable-web-search",
        "--deny",
        "*",
        "--disallowed-tools",
        "*",
        "--leader-socket",
        command[15],
        "--model",
        "grok-4",
    )
    assert command[command.index("--reasoning-effort") + 1] == "high"
    assert command[-2:] == ("--single", f"system\n\n{ONE_SHOT_DIRECTIVE}\n\nexplain")
    assert {"--acp", "--session-id", "--resume", "--continue", "-r", "-c"}.isdisjoint(command)
    assert leader_socket_parent_exists == [True]
    # One-shot generation runs in a neutral temp dir, never the request's project cwd.
    assert cwds[0] != "/tmp/project"
    assert cwds[0] is not None and "gobby-textgen-" in cwds[0]


@pytest.mark.asyncio
async def test_droid_cli_text_generate_adapter_executes_noninteractive_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    calls: list[tuple[tuple[str, ...], str | None, dict[str, str]]] = []

    async def fake_create_subprocess_exec(
        *command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        assert start_new_session is True
        calls.append((command, cwd, env))
        return FakeProcess(b"done\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(command_path="/usr/local/bin/droid")

    response = await adapter.generate(
        TextGenerationRequest(
            prompt="explain",
            system_prompt="system",
            model="claude-opus-4-7",
            reasoning_effort="high",
            cwd="/tmp/project",
        )
    )

    assert response == "done"
    command, cwd, env = calls[0]
    assert command == (
        "/usr/local/bin/droid",
        "exec",
        "--output-format",
        "text",
        "--model",
        "claude-opus-4-7",
        "--reasoning-effort",
        "high",
        "system\n\nexplain",
    )
    # One-shot generation runs in a neutral temp dir, never the request's project cwd.
    assert cwd != "/tmp/project"
    assert cwd is not None and "gobby-textgen-" in cwd
    temp_home = _assert_droid_isolated_env(env)
    assert not temp_home.exists()


@pytest.mark.asyncio
async def test_droid_cli_text_generate_adapter_reports_exec_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    temp_homes: list[Path] = []

    async def fake_create_subprocess_exec(
        *_command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        assert start_new_session is True
        temp_homes.append(Path(env["HOME"]))
        return FakeProcess(b"", b"bad auth", returncode=2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(command_path="/usr/local/bin/droid")

    with pytest.raises(RuntimeError, match="bad auth.*set FACTORY_API_KEY"):
        await adapter.generate(TextGenerationRequest(prompt="hello"))

    assert temp_homes
    assert all(not temp_home.exists() for temp_home in temp_homes)


@pytest.mark.asyncio
async def test_droid_cli_text_generate_adapter_reports_timeout_with_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    process = HangingProcess()
    temp_homes: list[Path] = []

    async def fake_create_subprocess_exec(
        *_command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        assert start_new_session is True
        temp_homes.append(Path(env["HOME"]))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(
        command_path="/usr/local/bin/droid",
        timeout_seconds=0.01,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await adapter.generate(TextGenerationRequest(prompt="hello world"))

    assert process.terminated is True
    assert process.killed is False
    assert "Droid exec timed out after 0.01s" in str(exc_info.value)
    assert "/usr/local/bin/droid exec --output-format text 'hello world'" in str(exc_info.value)
    assert temp_homes
    assert all(not temp_home.exists() for temp_home in temp_homes)


@pytest.mark.asyncio
async def test_droid_cli_text_generate_adapter_cleans_temp_home_after_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    temp_homes: list[Path] = []

    async def fake_create_subprocess_exec(
        *_command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        assert start_new_session is True
        temp_home = Path(env["HOME"])
        temp_homes.append(temp_home)
        (temp_home / ".factory" / "sessions").mkdir(parents=True)
        (temp_home / ".factory" / "sessions" / "started.jsonl").write_text(
            "{}\n",
            encoding="utf-8",
        )
        raise OSError("exec setup failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(command_path="/usr/local/bin/droid")

    with pytest.raises(OSError, match="exec setup failed"):
        await adapter.generate(TextGenerationRequest(prompt="hello"))

    assert temp_homes
    assert all(not temp_home.exists() for temp_home in temp_homes)


@pytest.mark.asyncio
async def test_droid_cli_text_generate_adapter_seeds_auth_config_without_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_home = tmp_path / "real-home"
    source_factory = real_home / ".factory"
    allowed_files = {
        "auth.v2.file",
        "auth.v2.key",
        "cache/certs/factory-cli-certs.pem",
        "certs/system-certs-cache.json",
        "cli-hints.json",
        "droids/worker.md",
        "hooks/hooks.json",
        "host.json",
        "mcp.json",
        "plugins/installed_plugins.json",
        "plugins/marketplaces/factory-plugins/index.json",
    }
    excluded_files = {
        "background-processes.json",
        "background-tasks.json",
        "bin/rg",
        "cache/search/manifest.json",
        "cache/session-discovery-index.json",
        "history.json",
        "logs/console.log",
        "plugins/cache/factory-plugins/package.json",
        "sessions/project/session.jsonl",
        "telemetry/events.json",
        "temp/runtime.json",
    }
    for relative_file in allowed_files | excluded_files:
        file_path = source_factory / relative_file
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(relative_file, encoding="utf-8")

    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    copied_files: set[str] = set()
    temp_homes: list[Path] = []

    async def fake_create_subprocess_exec(
        *_command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        assert start_new_session is True
        temp_home = Path(env["HOME"])
        temp_homes.append(temp_home)
        seeded_factory = temp_home / ".factory"
        copied_files.update(
            sorted(
                str(path.relative_to(seeded_factory))
                for path in seeded_factory.rglob("*")
                if path.is_file()
            )
        )
        return FakeProcess(b"done\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(command_path="/usr/local/bin/droid")

    response = await adapter.generate(TextGenerationRequest(prompt="hello"))

    assert response == "done"
    assert allowed_files <= copied_files
    assert copied_files.isdisjoint(excluded_files)
    assert temp_homes
    assert all(not temp_home.exists() for temp_home in temp_homes)


def test_is_feature_generation_infrastructure_error_classification() -> None:
    assert is_feature_generation_infrastructure_error(FeatureGenerationUnavailableError("x"))
    assert is_feature_generation_infrastructure_error(_CandidateTimeoutError("t"))
    assert is_feature_generation_infrastructure_error(
        CapabilityUnavailableError(AICapability.TEXT_GENERATE)
    )
    # A single malformed-JSON parse failure is an ordinary candidate failure, not infra.
    assert not is_feature_generation_infrastructure_error(ValueError("bad json"))
    assert not is_feature_generation_infrastructure_error(None)
    # The cause/context chain is walked.
    try:
        try:
            raise _CandidateTimeoutError("inner timeout")
        except _CandidateTimeoutError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as exc:
        assert is_feature_generation_infrastructure_error(exc)


@pytest.mark.asyncio
async def test_generate_json_raises_typed_infra_error_when_all_candidates_fail() -> None:
    registry = _two_candidate_registry("local:a", "local:b")
    service = TextGenerationService(
        registry,
        {
            "local:a": ProviderFailureAdapter(_CandidateTimeoutError("a timed out")),
            "local:b": ProviderFailureAdapter(_CandidateTimeoutError("b timed out")),
        },
    )

    with pytest.raises(FeatureGenerationUnavailableError) as exc_info:
        await service.generate_json(
            TextGenerationRequest(
                prompt="extract",
                candidates=("local:a/slow-model", "local:b/good-model"),
            )
        )
    # The terminal failure is classified as infrastructure (callers back off, not reject).
    assert is_feature_generation_infrastructure_error(exc_info.value)


def test_candidate_timeout_selection_by_adapter_style() -> None:
    service = TextGenerationService(
        AICapabilityRegistry([]),
        candidate_timeout_seconds=60.0,
        cli_candidate_timeout_seconds=150.0,
    )
    default_request = TextGenerationRequest(prompt="prompt")

    def _binding(style: AIAdapterStyle) -> CapabilityBinding:
        return CapabilityBinding(
            capability=AICapability.TEXT_GENERATE,
            provider="p",
            adapter_style=style,
            available=True,
        )

    # Spawn-cold lanes get the larger CLI timeout.
    for style in (
        AIAdapterStyle.CLI,
        AIAdapterStyle.DAEMON,
        AIAdapterStyle.LLM_PROVIDER,
        AIAdapterStyle.ACP,
    ):
        assert service._candidate_timeout_for_binding(default_request, _binding(style)) == 150.0

    # Fast API lanes keep the tight candidate timeout.
    for style in (AIAdapterStyle.LOCAL, AIAdapterStyle.OPENAI_COMPATIBLE):
        assert service._candidate_timeout_for_binding(default_request, _binding(style)) == 60.0

    # No binding falls back to the fast timeout.
    assert service._candidate_timeout_for_binding(default_request, None) == 60.0

    cli_override_request = TextGenerationRequest(
        prompt="prompt",
        cli_candidate_timeout_seconds=180.0,
    )
    assert (
        service._candidate_timeout_for_binding(
            cli_override_request,
            _binding(AIAdapterStyle.CLI),
        )
        == 180.0
    )
    assert (
        service._candidate_timeout_for_binding(
            cli_override_request,
            _binding(AIAdapterStyle.LOCAL),
        )
        == 60.0
    )

    fast_override_request = TextGenerationRequest(
        prompt="prompt",
        candidate_timeout_seconds=12.0,
    )
    assert (
        service._candidate_timeout_for_binding(
            fast_override_request,
            _binding(AIAdapterStyle.LOCAL),
        )
        == 12.0
    )
    assert (
        service._candidate_timeout_for_binding(
            fast_override_request,
            _binding(AIAdapterStyle.CLI),
        )
        == 150.0
    )

    both_override_request = TextGenerationRequest(
        prompt="prompt",
        candidate_timeout_seconds=12.0,
        cli_candidate_timeout_seconds=180.0,
    )
    assert (
        service._candidate_timeout_for_binding(
            both_override_request,
            _binding(AIAdapterStyle.LOCAL),
        )
        == 12.0
    )
    assert (
        service._candidate_timeout_for_binding(
            both_override_request,
            _binding(AIAdapterStyle.CLI),
        )
        == 180.0
    )


@pytest.mark.asyncio
async def test_run_cli_text_generation_command_closes_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(
        *command: str,
        stdin: int,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
        start_new_session: bool,
    ) -> FakeProcess:
        captured["stdin"] = stdin
        captured["cwd"] = cwd
        return FakeProcess(b"ok\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await text_generation_adapters._run_cli_text_generation_command(
        "Qwen",
        ("/usr/local/bin/qwen", "--prompt", "hi"),
        neutral_cwd=tmp_path,
        timeout_seconds=5,
        env_overrides={},
    )

    assert result == "ok"
    # stdin is closed so codex-style "Reading additional input from stdin" cannot hang.
    assert captured["stdin"] == asyncio.subprocess.DEVNULL
    assert captured["cwd"] == str(tmp_path)
