from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from gobby.adapters.acp_client import StreamEvent
from gobby.ai import (
    ACPTextGenerateAdapter,
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityUnavailableError,
    ClaudeTextGenerateAdapter,
    CodexAppServerTextGenerateAdapter,
    DroidCLITextGenerateAdapter,
    LocalTextGenerateAdapter,
    TextGenerationRequest,
    TextGenerationService,
    build_daemon_text_generation_service,
)
from gobby.ai.text_generation import ONE_SHOT_DIRECTIVE
from gobby.config.app import DaemonConfig
from gobby.config.feature_base import FeatureProfile
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


@pytest.mark.asyncio
async def test_text_generation_service_selects_available_registry_binding() -> None:
    providers = {
        "claude": AIAdapterStyle.LLM_PROVIDER,
        "codex": AIAdapterStyle.DAEMON,
        "local:lm-studio": AIAdapterStyle.OPENAI_COMPATIBLE,
        "gemini": AIAdapterStyle.ACP,
        "grok": AIAdapterStyle.ACP,
        "qwen": AIAdapterStyle.ACP,
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
async def test_recoverable_candidate_failure_logs_feature_llm_call_at_warning(
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
    assert [record.levelno for record in records] == [logging.WARNING, logging.DEBUG]
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
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.3-codex-spark",),
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
    codex = StaticTextAdapter(prompt)
    claude = RecordingAdapter("claude")
    service = TextGenerationService(registry, {"codex": codex, "claude": claude})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt=prompt,
            profile="feature_mid",
            candidates=("codex/gpt-5.3-codex-spark", "claude/sonnet"),
        )
    )

    assert result.text == f"claude:{prompt}"
    assert result.provider == "claude"
    assert result.model == "sonnet"
    assert codex.requests[0].model == "gpt-5.3-codex-spark"
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
                provider="codex",
                adapter_style=AIAdapterStyle.DAEMON,
                available=True,
                models=("gpt-5.3-codex-spark",),
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
    codex = StaticTextAdapter(f"{echoed_prefix}\n\nGenerated prose after an echoed prompt.")
    claude = RecordingAdapter("claude")
    service = TextGenerationService(registry, {"codex": codex, "claude": claude})

    result = await service.generate_result(
        TextGenerationRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            profile="feature_mid",
            candidates=("codex/gpt-5.3-codex-spark", "claude/sonnet"),
        )
    )

    assert result.text == f"claude:{prompt}"
    assert result.provider == "claude"
    assert result.model == "sonnet"
    assert codex.requests[0].model == "gpt-5.3-codex-spark"
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
    service = TextGenerationService(registry, {"codex": codex})

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
                    FeatureProfile.LOW: ["local:lm-studio/qwen-local"],
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
async def test_text_generation_service_resolves_only_selected_adapter() -> None:
    providers = ("claude", "codex", "local:lm-studio", "gemini", "grok", "qwen", "droid")
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
    providers = ("claude", "codex", "local:lm-studio", "gemini", "grok", "qwen", "droid")
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


class FakeNativeTextProvider:
    last_instance: ClassVar[FakeNativeTextProvider | None] = None

    def __init__(self, config: DaemonConfig, endpoint_name: str | None = None) -> None:
        self.config = config
        self.endpoint_name = endpoint_name
        self.text_calls: list[tuple[str, str | None, str | None, int | None, str | None]] = []
        self.json_calls: list[tuple[str, str | None, str | None, str | None]] = []
        self.__class__.last_instance = self

    async def generate_text_result(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        *,
        caller: str | None = None,
    ) -> LLMTextResult:
        self.text_calls.append((prompt, system_prompt, model, max_tokens, caller))
        return LLMTextResult(
            text=f"{system_prompt}:{prompt}:{model}:{max_tokens}:{caller}",
            usage={"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
        )

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        *,
        caller: str | None = None,
    ) -> dict[str, Any]:
        self.json_calls.append((prompt, system_prompt, model, caller))
        return {
            "prompt": prompt,
            "system_prompt": system_prompt,
            "model": model,
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
            caller="test",
        )
    )

    provider = FakeNativeTextProvider.last_instance
    assert provider is not None
    assert provider.config is config
    assert provider.text_calls == [("hello", "system", "model-a", 42, "test")]
    assert response.text == "system:hello:model-a:42:test"
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
            caller="test",
        )
    )

    provider = FakeNativeTextProvider.last_instance
    assert provider is not None
    assert provider.config is config
    assert provider.endpoint_name == "lm-studio"
    assert provider.json_calls == [("json please", "system", "model-b", "test")]
    assert response == {
        "prompt": "json please",
        "system_prompt": "system",
        "model": "model-b",
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
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.started = False
        self.stopped = False
        self.thread_kwargs: dict[str, object] | None = None
        self.turn_kwargs: dict[str, object] | None = None
        self.events = events or [
            {"type": "item/agentMessage/delta", "delta": "hello "},
            {
                "type": "item/completed",
                "item": {"content": [{"text": "ignored fallback"}]},
            },
            {"type": "item/agentMessage/delta", "delta": "world"},
        ]

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def start_thread(
        self,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        terminal_context: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.thread_kwargs = {
            "cwd": cwd,
            "model": model,
            "approval_policy": approval_policy,
            "sandbox": sandbox,
        }
        return SimpleNamespace(id="thread-1")

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
        for event in self.events:
            yield event


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
        await asyncio.Event().wait()
        yield {"type": "item/agentMessage/delta", "delta": "unreachable"}


@pytest.mark.asyncio
async def test_codex_app_server_text_generate_adapter_runs_one_shot_turn() -> None:
    client = FakeCodexAppServerClient()
    adapter = CodexAppServerTextGenerateAdapter(lambda: client)

    response = await adapter.generate(
        TextGenerationRequest(
            provider="codex",
            prompt="user prompt",
            system_prompt="system prompt",
            model="gpt-5.4",
            cwd="/tmp/project",
        )
    )

    assert response == "hello world"
    assert client.started is True
    assert client.stopped is True
    assert client.thread_kwargs == {
        "cwd": "/tmp/project",
        "model": "gpt-5.4",
        "approval_policy": "never",
        "sandbox": "readOnly",
    }
    assert client.turn_kwargs == {
        "thread_id": "thread-1",
        "prompt": "user prompt",
        "images": None,
        "context_prefix": f"system prompt\n\n{ONE_SHOT_DIRECTIVE}",
    }


@pytest.mark.asyncio
async def test_codex_app_server_text_generate_adapter_ignores_completed_user_messages() -> None:
    client = FakeCodexAppServerClient(
        [
            {
                "type": "item/completed",
                "item": {"type": "userMessage", "content": [{"text": "user prompt"}]},
            },
            {
                "type": "item/completed",
                "item": {"type": "plan", "text": "plan text"},
            },
            {
                "type": "item/completed",
                "item": {"type": "agentMessage", "text": "final answer"},
            },
        ]
    )
    adapter = CodexAppServerTextGenerateAdapter(lambda: client)

    response = await adapter.generate(
        TextGenerationRequest(
            provider="codex",
            prompt="user prompt",
            model="gpt-5.3-codex-spark",
        )
    )

    assert response == "final answer"


@pytest.mark.asyncio
async def test_codex_app_server_text_generate_adapter_raises_on_completed_error() -> None:
    client = FakeCodexAppServerClient(
        [
            {
                "type": "turn/completed",
                "turn": {
                    "id": "turn-1",
                    "status": "error",
                    "error": "quota exceeded",
                    "items": [],
                },
            }
        ]
    )
    adapter = CodexAppServerTextGenerateAdapter(lambda: client)

    with pytest.raises(RuntimeError, match="quota exceeded"):
        await adapter.generate(TextGenerationRequest(provider="codex", prompt="user prompt"))

    assert client.stopped is True


@pytest.mark.asyncio
async def test_codex_app_server_text_generate_adapter_times_out_and_stops_client() -> None:
    client = HangingCodexAppServerClient()
    adapter = CodexAppServerTextGenerateAdapter(lambda: client, timeout_seconds=0.01)

    with pytest.raises(RuntimeError, match="timed out after 0.01s"):
        await adapter.generate(TextGenerationRequest(provider="codex", prompt="user prompt"))

    assert client.started is True
    assert client.stopped is True
    assert client.turn_kwargs == {
        "thread_id": "thread-1",
        "prompt": "user prompt",
        "images": None,
        "context_prefix": ONE_SHOT_DIRECTIVE,
    }


@pytest.mark.asyncio
async def test_codex_app_server_text_generate_adapter_enforces_configured_deadline() -> None:
    client = HangingCodexAppServerClient()
    adapter = CodexAppServerTextGenerateAdapter(lambda: client, timeout_seconds=0.01)

    with pytest.raises(RuntimeError, match="timed out after 0.01s"):
        await adapter.generate(TextGenerationRequest(provider="codex", prompt="never completes"))

    assert client.started is True
    assert client.stopped is True


@pytest.mark.asyncio
async def test_daemon_codex_text_generate_adapter_uses_configured_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HangingCodexAppServerClient()
    monkeypatch.setattr("gobby.ai.text_generation._codex_app_server_client", lambda: client)
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

    with pytest.raises(RuntimeError, match="timed out after 0.01s"):
        await service.generate(
            TextGenerationRequest(
                provider="codex",
                model="gpt-5",
                prompt="never completes",
            )
        )

    assert client.started is True
    assert client.stopped is True


@pytest.mark.asyncio
async def test_codex_app_server_text_generate_adapter_raises_when_turn_has_no_output() -> None:
    client = FakeCodexAppServerClient(
        [{"type": "turn/completed", "turn": {"id": "turn-1", "status": "completed", "items": []}}]
    )
    adapter = CodexAppServerTextGenerateAdapter(lambda: client)

    with pytest.raises(RuntimeError, match="returned no output"):
        await adapter.generate(TextGenerationRequest(provider="codex", prompt="user prompt"))

    assert client.stopped is True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "grok", "qwen"])
async def test_acp_text_generate_adapter_runs_one_shot_prompt_turn(provider: str) -> None:
    client = FakeACPClient()
    adapter = ACPTextGenerateAdapter(lambda: client)  # type: ignore[arg-type]

    response = await adapter.generate(
        TextGenerationRequest(
            provider=provider,
            prompt="user prompt",
            system_prompt="system prompt",
            model="model-a",
            cwd="/tmp/project",
        )
    )

    assert response == "hello world"
    assert client.started == {
        "auto_session": True,
        "cwd": "/tmp/project",
        "model": "model-a",
    }
    assert len(client.sent) == 1
    sent = dict(client.sent[0])
    pre_tool_callback = sent.pop("pre_tool_callback")
    assert sent == {
        "message": f"system prompt\n\n{ONE_SHOT_DIRECTIVE}\n\nuser prompt",
        "model": "model-a",
    }
    decision = await pre_tool_callback({"tool_name": "read_file", "tool_input": {}})
    assert decision == {
        "decision": "deny",
        "reason": "Tool use is disabled for one-shot text generation.",
    }
    assert client.stopped is True


@pytest.mark.asyncio
async def test_text_generation_service_falls_back_when_acp_candidate_errors() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="gemini",
                adapter_style=AIAdapterStyle.ACP,
                available=True,
                models=("gemini-pro",),
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
    acp_client = FakeACPClient(
        [StreamEvent(event_type="error", data={"message": "provider unavailable"})]
    )
    claude = RecordingAdapter("claude")
    service = TextGenerationService(
        registry,
        {
            "gemini": ACPTextGenerateAdapter(lambda: acp_client),  # type: ignore[arg-type]
            "claude": claude,
        },
    )

    result = await service.generate_result(
        TextGenerationRequest(
            prompt="summarize",
            profile="feature_low",
            candidates=("gemini/gemini-pro", "claude/haiku"),
        )
    )

    assert result.text == "claude:summarize"
    assert acp_client.stopped is True


@pytest.mark.asyncio
async def test_acp_one_shot_directive_composes_with_json_instruction() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="gemini",
                adapter_style=AIAdapterStyle.ACP,
                available=True,
                models=("gemini-pro",),
            )
        ]
    )
    acp_client = FakeACPClient(
        [StreamEvent(event_type="content_delta", data={"content": '{"ok": true}'})]
    )
    service = TextGenerationService(
        registry,
        {"gemini": ACPTextGenerateAdapter(lambda: acp_client)},  # type: ignore[dict-item]
    )

    result = await service.generate_json(
        TextGenerationRequest(
            prompt="classify",
            system_prompt="caller prompt",
            provider="gemini",
            model="gemini-pro",
        )
    )

    assert result == {"ok": True}
    message = str(acp_client.sent[0]["message"])
    assert message.startswith("caller prompt")
    json_instruction_index = message.index("Respond with a single valid JSON object")
    directive_index = message.index(ONE_SHOT_DIRECTIVE)
    assert json_instruction_index < directive_index


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
    assert [record.levelno for record in records] == [logging.WARNING, logging.DEBUG]
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
    client = HangingACPClient()
    monkeypatch.setattr("gobby.ai.text_generation._gemini_acp_client", lambda: client)
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="gemini",
                adapter_style=AIAdapterStyle.ACP,
                available=True,
            )
        ]
    )
    service = build_daemon_text_generation_service(
        DaemonConfig(ai={"generation": {"candidate_timeout_seconds": 0.01}}),
        registry=registry,
    )

    with pytest.raises(RuntimeError, match="candidate timed out after 0.01s"):
        await service.generate(
            TextGenerationRequest(provider="gemini", model="gemini-pro", prompt="never completes")
        )

    assert client.stopped is True


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int | None = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

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
async def test_droid_cli_text_generate_adapter_executes_noninteractive_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_create_subprocess_exec(
        *command: str,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
    ) -> FakeProcess:
        calls.append(
            {
                "command": command,
                "stdout": stdout,
                "stderr": stderr,
                "cwd": cwd,
                "env": env,
            }
        )
        return FakeProcess(b"done\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(command_path="/usr/local/bin/droid")

    response = await adapter.generate(
        TextGenerationRequest(
            prompt="explain",
            system_prompt="system",
            model="claude-opus-4-7",
            cwd="/tmp/project",
        )
    )

    assert response == "done"
    assert calls[0]["command"] == (
        "/usr/local/bin/droid",
        "exec",
        "--output-format",
        "text",
        "--model",
        "claude-opus-4-7",
        "system\n\nexplain",
    )
    assert calls[0]["cwd"] == "/tmp/project"
    assert calls[0]["env"]["GOBBY_HOOKS_DISABLED"] == "1"  # type: ignore[index]


@pytest.mark.asyncio
async def test_droid_cli_text_generate_adapter_reports_exec_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_subprocess_exec(
        *_command: str,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
    ) -> FakeProcess:
        return FakeProcess(b"", b"bad auth", returncode=2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(command_path="/usr/local/bin/droid")

    with pytest.raises(RuntimeError, match="Droid exec failed with exit code 2: bad auth"):
        await adapter.generate(TextGenerationRequest(prompt="hello"))


@pytest.mark.asyncio
async def test_droid_cli_text_generate_adapter_reports_timeout_with_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = HangingProcess()

    async def fake_create_subprocess_exec(
        *_command: str,
        stdout: int,
        stderr: int,
        cwd: str | None,
        env: dict[str, str],
    ) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = DroidCLITextGenerateAdapter(
        command_path="/usr/local/bin/droid",
        timeout_seconds=0.01,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await adapter.generate(TextGenerationRequest(prompt="hello world"))

    assert process.killed is True
    assert "Droid exec timed out after 0.01s" in str(exc_info.value)
    assert "/usr/local/bin/droid exec --output-format text 'hello world'" in str(exc_info.value)
