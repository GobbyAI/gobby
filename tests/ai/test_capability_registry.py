from __future__ import annotations

import pytest

from gobby.ai import (
    CANONICAL_AI_CAPABILITIES,
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityUnavailableError,
    build_daemon_ai_capability_registry,
)
from gobby.config.app import DaemonConfig
from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig
from gobby.config.local import LocalConfig
from gobby.config.persistence import EmbeddingsConfig
from gobby.config.voice import OpenAICompatibleAudioBindingConfig, VoiceConfig
from gobby.providers import AGY_UNAVAILABLE_REASON, ProviderMetadata

pytestmark = pytest.mark.unit


def test_empty_registry_advertises_every_canonical_capability() -> None:
    registry = AICapabilityRegistry()

    assert registry.capabilities == CANONICAL_AI_CAPABILITIES
    snapshot = registry.status_snapshot()
    assert tuple(snapshot["capabilities"]) == tuple(
        capability.value for capability in CANONICAL_AI_CAPABILITIES
    )

    for capability in CANONICAL_AI_CAPABILITIES:
        status = registry.status(capability)
        assert status.available is False
        assert status.to_dict()["state"] == "unavailable"
        assert status.reason is not None
        assert capability.value in status.reason


def test_registry_tracks_available_and_unavailable_bindings() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="claude",
                adapter_style=AIAdapterStyle.LLM_PROVIDER,
                available=True,
                models=("sonnet",),
            ),
            CapabilityBinding.unavailable(
                AICapability.TEXT_GENERATE,
                "agy",
                reason=AGY_UNAVAILABLE_REASON,
            ),
        ]
    )

    status = registry.status("text_generate")

    assert status.available is True
    assert registry.select(AICapability.TEXT_GENERATE, model="sonnet").provider == "claude"
    agy = registry.binding(AICapability.TEXT_GENERATE, "agy")
    assert agy is not None
    assert agy.available is False
    assert agy.reason == AGY_UNAVAILABLE_REASON


def test_select_reports_unavailable_provider_reason() -> None:
    registry = AICapabilityRegistry(
        [
            CapabilityBinding.unavailable(
                AICapability.VISION_EXTRACT,
                "agy",
                reason=AGY_UNAVAILABLE_REASON,
            )
        ]
    )

    with pytest.raises(CapabilityUnavailableError, match="AGY has no documented"):
        registry.select(AICapability.VISION_EXTRACT, provider="agy")


def test_daemon_registry_keeps_web_chat_and_text_generate_separate() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(),
        provider_installed=lambda _entry: True,
    )

    text_generate = registry.status(AICapability.TEXT_GENERATE)
    web_chat = registry.status(AICapability.WEB_CHAT)

    assert text_generate.capability == AICapability.TEXT_GENERATE
    assert web_chat.capability == AICapability.WEB_CHAT
    text_binding = registry.binding(AICapability.TEXT_GENERATE, "claude")
    web_chat_binding = registry.binding(AICapability.WEB_CHAT, "claude")
    assert text_binding is not None
    assert text_binding.adapter_style == AIAdapterStyle.LLM_PROVIDER
    assert web_chat_binding is not None
    assert web_chat_binding.adapter_style == AIAdapterStyle.CLI


def test_daemon_registry_reports_text_generate_provider_bindings() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(
            local=LocalConfig(url="http://localhost:1234/v1", model="llama"),
            llm_providers=LLMProvidersConfig(
                claude=LLMProviderConfig(
                    models="haiku,sonnet",
                    default_model="sonnet",
                ),
                codex=LLMProviderConfig(
                    models="gpt-5.4,gpt-5.3-codex",
                    default_model="gpt-5.4",
                    auth_mode="api_key",
                ),
            ),
        ),
        provider_installed=lambda _entry: True,
    )

    expected_styles = {
        "claude": AIAdapterStyle.LLM_PROVIDER,
        "codex": AIAdapterStyle.LLM_PROVIDER,
        "local": AIAdapterStyle.OPENAI_COMPATIBLE,
        "gemini": AIAdapterStyle.ACP,
        "grok": AIAdapterStyle.ACP,
        "qwen": AIAdapterStyle.ACP,
        "droid": AIAdapterStyle.CLI,
    }

    for provider, adapter_style in expected_styles.items():
        binding = registry.select(AICapability.TEXT_GENERATE, provider=provider)
        assert binding.provider == provider
        assert binding.adapter_style == adapter_style

    local = registry.binding(AICapability.TEXT_GENERATE, "local")
    assert local is not None
    assert local.models == ("llama",)

    claude = registry.binding(AICapability.TEXT_GENERATE, "claude")
    assert claude is not None
    assert claude.models == ("haiku", "sonnet")
    assert claude.metadata["default_model"] == "sonnet"

    codex = registry.binding(AICapability.TEXT_GENERATE, "codex")
    assert codex is not None
    assert codex.models == ("gpt-5.4", "gpt-5.3-codex")
    assert codex.metadata["default_model"] == "gpt-5.4"
    assert codex.metadata["auth_mode"] == "api_key"


def test_daemon_registry_reports_only_proven_vision_extract_bindings_available() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(
            local=LocalConfig(url="http://localhost:1234/v1", model="llava"),
            llm_providers=LLMProvidersConfig(
                codex=LLMProviderConfig(models="gpt-5-vision", default_model="gpt-5-vision")
            ),
        ),
        provider_installed=lambda _entry: True,
    )

    status = registry.status(AICapability.VISION_EXTRACT)
    available_providers = {binding.provider for binding in status.bindings if binding.available}

    assert available_providers == {"claude", "codex", "local"}

    local = registry.binding(AICapability.VISION_EXTRACT, "local")
    assert local is not None
    assert local.adapter_style == AIAdapterStyle.OPENAI_COMPATIBLE
    assert local.models == ("llava",)

    codex = registry.binding(AICapability.VISION_EXTRACT, "codex")
    assert codex is not None
    assert codex.models == ("gpt-5-vision",)
    assert codex.metadata["default_model"] == "gpt-5-vision"

    for provider in ("droid", "gemini", "grok", "qwen"):
        binding = registry.binding(AICapability.VISION_EXTRACT, provider)
        assert binding is not None
        assert binding.available is False
        assert binding.reason is not None
        assert "proven image payload support" in binding.reason


def test_daemon_registry_marks_agy_unavailable_even_when_provider_probe_succeeds() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(),
        provider_installed=lambda _entry: True,
    )

    for capability in (
        AICapability.TEXT_GENERATE,
        AICapability.VISION_EXTRACT,
        AICapability.AGENT_SPAWN,
        AICapability.WEB_CHAT,
    ):
        binding = registry.binding(capability, "agy")
        assert binding is not None
        assert binding.available is False
        assert binding.reason == AGY_UNAVAILABLE_REASON


def test_daemon_registry_reports_embedding_configured_state() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(embeddings=EmbeddingsConfig(api_base="http://localhost:11434/v1")),
        provider_installed=lambda _entry: False,
    )

    binding = registry.select(AICapability.EMBED)

    assert binding.provider == "local"
    assert binding.adapter_style == AIAdapterStyle.OPENAI_COMPATIBLE
    assert binding.models == ("nomic-embed-text",)
    assert binding.metadata["dim"] == 768


def test_daemon_registry_reports_voice_transcribe_configured_state() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(voice=VoiceConfig(enabled=True, stt_enabled=True)),
        provider_installed=lambda _entry: False,
    )

    binding = registry.select(AICapability.AUDIO_TRANSCRIBE)

    assert binding.provider == "whisper"
    assert binding.adapter_style == AIAdapterStyle.LOCAL
    assert binding.models == ("base",)

    translate = registry.select(AICapability.AUDIO_TRANSLATE)
    assert translate.provider == "whisper"
    assert translate.adapter_style == AIAdapterStyle.LOCAL
    assert translate.models == ("base",)


def test_daemon_registry_reports_openai_compatible_audio_bindings() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(
            voice=VoiceConfig(
                enabled=True,
                openai_compatible_audio=[
                    OpenAICompatibleAudioBindingConfig(
                        provider="remote-stt",
                        url="http://localhost:8080/v1",
                        model="whisper-large-v3",
                    )
                ],
            )
        ),
        provider_installed=lambda _entry: False,
    )

    for capability in (AICapability.AUDIO_TRANSCRIBE, AICapability.AUDIO_TRANSLATE):
        binding = registry.select(capability, provider="remote-stt")
        assert binding.adapter_style == AIAdapterStyle.OPENAI_COMPATIBLE
        assert binding.models == ("whisper-large-v3",)
        assert binding.metadata["url"] == "http://localhost:8080/v1"


def test_daemon_registry_reports_disabled_openai_audio_capability() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(
            voice=VoiceConfig(
                enabled=True,
                openai_compatible_audio=[
                    OpenAICompatibleAudioBindingConfig(
                        provider="remote-stt",
                        url="http://localhost:8080/v1",
                        model="whisper-large-v3",
                        translation_enabled=False,
                    )
                ],
            )
        ),
        provider_installed=lambda _entry: False,
    )

    binding = registry.binding(AICapability.AUDIO_TRANSLATE, "remote-stt")
    assert binding is not None
    assert binding.available is False
    assert binding.reason == "audio_translate is disabled for this OpenAI-compatible binding."


def test_daemon_registry_marks_missing_provider_binaries_unavailable() -> None:
    def installed(entry: ProviderMetadata) -> bool:
        return entry.provider == "claude"

    registry = build_daemon_ai_capability_registry(DaemonConfig(), provider_installed=installed)

    claude = registry.binding(AICapability.AGENT_SPAWN, "claude")
    codex = registry.binding(AICapability.AGENT_SPAWN, "codex")

    assert claude is not None
    assert claude.available is True
    assert codex is not None
    assert codex.available is False
    assert codex.reason == "Codex CLI is not installed."
