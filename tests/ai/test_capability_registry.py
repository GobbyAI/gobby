from __future__ import annotations

from typing import Any

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
from gobby.ai._tool_chat_builder import _local_client_factory
from gobby.config.ai import AIConfig, GenerationConfig
from gobby.config.app import DaemonConfig
from gobby.config.persistence import EmbeddingsConfig
from gobby.config.voice import OpenAICompatibleAudioBindingConfig, VoiceConfig
from gobby.llm.service import LLMService
from gobby.providers import AGY_UNAVAILABLE_REASON, ProviderMetadata
from gobby.servers.provider_model_defaults import AGY_MODELS

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


def _local_family_registry() -> AICapabilityRegistry:
    return AICapabilityRegistry(
        [
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:lm-studio",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-lm",),
            ),
            CapabilityBinding(
                capability=AICapability.TEXT_GENERATE,
                provider="endpoint:ollama",
                adapter_style=AIAdapterStyle.OPENAI_COMPATIBLE,
                available=True,
                models=("qwen-ollama",),
            ),
        ]
    )


def test_select_named_local_provider_does_not_match_other_endpoints() -> None:
    registry = _local_family_registry()

    with pytest.raises(
        CapabilityUnavailableError,
        match="provider 'endpoint:lm-studio' does not support requested model 'qwen-ollama'",
    ):
        registry.select(
            AICapability.TEXT_GENERATE, provider="endpoint:lm-studio", model="qwen-ollama"
        )


@pytest.mark.parametrize(
    ("provider", "adapter_style"),
    [
        ("codex", AIAdapterStyle.DAEMON),
        ("local", AIAdapterStyle.LOCAL),
    ],
)
def test_select_explicit_cli_backed_provider_model_bypasses_feature_model_list(
    provider: str, adapter_style: AIAdapterStyle
) -> None:
    binding = CapabilityBinding(
        capability=AICapability.TEXT_GENERATE,
        provider=provider,
        adapter_style=adapter_style,
        available=True,
        models=("allowlisted-default",),
    )
    registry = AICapabilityRegistry([binding])

    assert (
        registry.select(
            AICapability.TEXT_GENERATE,
            provider=provider,
            model="explicit-off-list-model",
        )
        is binding
    )


def test_strict_model_binding_rejects_explicit_model_override() -> None:
    binding = CapabilityBinding(
        capability=AICapability.TEXT_GENERATE,
        provider="agy",
        adapter_style=AIAdapterStyle.CLI,
        available=True,
        models=("gemini-3.5-flash",),
        strict_models=True,
    )
    registry = AICapabilityRegistry([binding])

    assert (
        registry.select(
            AICapability.TEXT_GENERATE,
            provider="agy",
            model="gemini-3.5-flash",
        )
        is binding
    )
    assert binding.accepts_explicit_model_override("unknown-model") is False
    assert binding.accepts_explicit_model_override("gemini-3.5-flash-low") is False

    with pytest.raises(CapabilityUnavailableError, match="requires an explicit supported model"):
        registry.select(AICapability.TEXT_GENERATE, provider="agy")
    with pytest.raises(CapabilityUnavailableError, match="does not support requested model"):
        registry.select(AICapability.TEXT_GENERATE, provider="agy", model="unknown-model")


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
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "lm-studio": {
                            "api_base": "http://localhost:1234/v1",
                            "model": "llama",
                        },
                        "ollama": {
                            "api_base": "http://localhost:11434/v1",
                            "model": "qwen2.5-coder",
                        },
                    }
                )
            ),
        ),
        provider_installed=lambda _entry: True,
    )

    expected_styles = {
        "claude": AIAdapterStyle.LLM_PROVIDER,
        "codex": AIAdapterStyle.DAEMON,
        "endpoint:lm-studio": AIAdapterStyle.OPENAI_COMPATIBLE,
        "endpoint:ollama": AIAdapterStyle.OPENAI_COMPATIBLE,
        "grok": AIAdapterStyle.CLI,
        "qwen": AIAdapterStyle.CLI,
        "droid": AIAdapterStyle.CLI,
    }

    for provider, adapter_style in expected_styles.items():
        binding = registry.select(AICapability.TEXT_GENERATE, provider=provider)
        assert binding.provider == provider
        assert binding.adapter_style == adapter_style

    assert registry.binding(AICapability.TEXT_GENERATE, "endpoint") is None

    lm_studio = registry.binding(AICapability.TEXT_GENERATE, "endpoint:lm-studio")
    assert lm_studio is not None
    assert lm_studio.models == ("llama",)
    assert lm_studio.metadata["endpoint"] == "lm-studio"

    claude = registry.binding(AICapability.TEXT_GENERATE, "claude")
    assert claude is not None
    assert claude.models == ("haiku", "opus", "sonnet")
    assert "fable" not in claude.models
    assert "default_model" not in claude.metadata
    assert "auth_mode" not in claude.metadata

    codex = registry.binding(AICapability.TEXT_GENERATE, "codex")
    assert codex is not None
    assert codex.models == (
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    )
    assert "default_model" not in codex.metadata
    assert "auth_mode" not in codex.metadata

    agy = registry.binding(AICapability.TEXT_GENERATE, "agy")
    assert agy is not None
    assert agy.available is True
    assert agy.adapter_style == AIAdapterStyle.CLI
    assert agy.strict_models is True
    assert agy.models == tuple(AGY_MODELS)
    assert agy.metadata["model_catalog_source"] == "agy-1.0.10-static"
    assert (
        registry.select(
            AICapability.TEXT_GENERATE,
            provider="agy",
            model="gemini-3.5-flash",
        )
        is agy
    )


def test_daemon_registry_registers_tool_chat_capability_per_style() -> None:
    registry = build_daemon_ai_capability_registry(
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

    # The llm_provider family (claude) is tool-capable today: available, with
    # the dispatch style recorded and a supports_tools flag for the filter.
    claude = registry.binding(AICapability.TOOL_CHAT, "claude")
    assert claude is not None
    assert claude.adapter_style == AIAdapterStyle.LLM_PROVIDER
    assert claude.available is True
    assert claude.metadata["supports_tools"] is True
    assert registry.select(AICapability.TOOL_CHAT, provider="claude") is claude

    # An openai_compatible local endpoint is tool-capable via the daemon loop.
    lm_studio = registry.binding(AICapability.TOOL_CHAT, "endpoint:lm-studio")
    assert lm_studio is not None
    assert lm_studio.adapter_style == AIAdapterStyle.OPENAI_COMPATIBLE
    assert lm_studio.available is True
    assert lm_studio.metadata["supports_tools"] is True


def test_daemon_registry_excludes_local_tool_chat_without_opt_in() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "lm-studio": {
                            "api_base": "http://localhost:1234/v1",
                            "model": "gemma",
                        },
                    }
                )
            ),
        ),
        provider_installed=lambda _entry: True,
    )

    assert registry.binding(AICapability.TOOL_CHAT, "endpoint:lm-studio") is None


def test_daemon_registry_excludes_not_yet_supported_tool_chat_styles() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(),
        provider_installed=lambda _entry: True,
    )
    # codex (daemon), droid (cli), grok (acp), and qwen (acp) all have
    # tool_chat spawn adapters that run gcode directly in-sandbox. With
    # providers installed, all four are available, tool-capable, and
    # selectable — provider-agnostic peers of claude/lm-studio.
    for provider, style in (
        ("codex", AIAdapterStyle.DAEMON),
        ("droid", AIAdapterStyle.CLI),
        ("grok", AIAdapterStyle.ACP),
        ("qwen", AIAdapterStyle.ACP),
    ):
        binding = registry.binding(AICapability.TOOL_CHAT, provider)
        assert binding is not None, provider
        assert binding.adapter_style == style, provider
        assert binding.available is True, provider
        assert binding.metadata["supports_tools"] is True, provider
        assert registry.select(AICapability.TOOL_CHAT, provider=provider) is binding

    # The capability is surfaced in status (a backward-compatible addition).
    status = registry.status(AICapability.TOOL_CHAT)
    assert status.capability == AICapability.TOOL_CHAT
    providers = {binding.provider for binding in status.bindings}
    assert {"claude", "codex", "droid", "grok", "qwen"}.issubset(providers)


def test_daemon_registry_matches_configured_claude_model_aliases() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(),
        provider_installed=lambda _entry: True,
    )

    haiku = registry.select(AICapability.TEXT_GENERATE, provider="claude", model="haiku")
    full_model = registry.select(
        AICapability.TEXT_GENERATE,
        provider="claude",
        model="claude-haiku-4-5",
    )
    provider_scoped = registry.select(
        AICapability.TEXT_GENERATE,
        provider="claude",
        model="claude/claude-haiku-4-5",
    )

    assert haiku.provider == "claude"
    assert full_model.provider == "claude"
    assert provider_scoped.provider == "claude"
    assert haiku.models == ("haiku", "opus", "sonnet")
    assert full_model.models == ("haiku", "opus", "sonnet")
    assert provider_scoped.models == ("haiku", "opus", "sonnet")
    assert "fable" not in haiku.models
    assert "default_model" not in haiku.metadata


def test_daemon_registry_applies_feature_models_to_provider_capabilities() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(),
        provider_installed=lambda _entry: True,
    )

    vision = registry.binding(AICapability.VISION_EXTRACT, "claude")
    agent = registry.binding(AICapability.AGENT_SPAWN, "claude")
    web = registry.binding(AICapability.WEB_CHAT, "claude")

    assert vision is not None
    assert agent is not None
    assert web is not None
    assert vision.models == ("haiku", "opus", "sonnet")
    assert agent.models == ("haiku", "opus", "sonnet")
    assert web.models == ("haiku", "opus", "sonnet")
    assert "fable" not in vision.models


def test_daemon_registry_reports_only_proven_vision_extract_bindings_available() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "lm-studio": {
                            "api_base": "http://localhost:1234/v1",
                            "model": "llava",
                            "probed_model": "llava",
                            "input_modalities": ["text", "image"],
                        }
                    }
                )
            ),
        ),
        provider_installed=lambda _entry: True,
    )

    status = registry.status(AICapability.VISION_EXTRACT)
    available_providers = {binding.provider for binding in status.bindings if binding.available}

    assert available_providers == {"claude", "endpoint:lm-studio"}

    assert registry.binding(AICapability.VISION_EXTRACT, "endpoint") is None

    lm_studio = registry.binding(AICapability.VISION_EXTRACT, "endpoint:lm-studio")
    assert lm_studio is not None
    assert lm_studio.available is True
    assert lm_studio.adapter_style == AIAdapterStyle.OPENAI_COMPATIBLE
    assert lm_studio.models == ("llava",)

    codex = registry.binding(AICapability.VISION_EXTRACT, "codex")
    assert codex is not None
    assert codex.available is False
    assert codex.adapter_style == AIAdapterStyle.DAEMON
    assert codex.reason is not None
    assert "proven image payload support" in codex.reason

    for provider in ("droid", "grok", "qwen"):
        binding = registry.binding(AICapability.VISION_EXTRACT, provider)
        assert binding is not None
        assert binding.available is False
        assert binding.reason is not None
        assert "proven image payload support" in binding.reason


def test_daemon_registry_scopes_agy_to_strict_text_generation_when_installed() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(),
        provider_installed=lambda _entry: True,
    )

    text_generate = registry.binding(AICapability.TEXT_GENERATE, "agy")
    assert text_generate is not None
    assert text_generate.available is True
    assert text_generate.adapter_style == AIAdapterStyle.CLI
    assert text_generate.strict_models is True
    assert text_generate.models == tuple(AGY_MODELS)

    with pytest.raises(CapabilityUnavailableError, match="requires an explicit supported model"):
        registry.select(AICapability.TEXT_GENERATE, provider="agy")
    with pytest.raises(CapabilityUnavailableError, match="does not support requested model"):
        registry.select(AICapability.TEXT_GENERATE, provider="agy", model="bad-model")

    for capability in (
        AICapability.VISION_EXTRACT,
        AICapability.AGENT_SPAWN,
        AICapability.WEB_CHAT,
    ):
        binding = registry.binding(capability, "agy")
        assert binding is not None
        assert binding.available is False
        assert binding.reason == AGY_UNAVAILABLE_REASON


def test_daemon_registry_marks_agy_text_generation_unavailable_when_cli_absent() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(),
        provider_installed=lambda entry: entry.provider != "agy",
    )

    binding = registry.binding(AICapability.TEXT_GENERATE, "agy")
    assert binding is not None
    assert binding.available is False
    assert binding.reason == "AGY CLI is not installed."
    assert binding.strict_models is True
    assert binding.models == tuple(AGY_MODELS)


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


def test_daemon_registry_reports_voice_transcribe_configured_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.ai.registry._whisper_runtime_available", lambda _config: True)
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


def test_daemon_registry_reports_whisper_runtime_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.ai.registry._whisper_runtime_available", lambda _config: False)
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(
            voice=VoiceConfig(
                enabled=True,
                stt_enabled=True,
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

    whisper = registry.binding(AICapability.AUDIO_TRANSCRIBE, "whisper")
    assert whisper is not None
    assert whisper.available is False
    assert whisper.reason == "faster-whisper is not installed."
    assert registry.select(AICapability.AUDIO_TRANSCRIBE).provider == "remote-stt"

    audio_status = registry.status_snapshot()["capabilities"][AICapability.AUDIO_TRANSCRIBE.value]
    whisper_status = next(
        binding for binding in audio_status["bindings"] if binding["provider"] == "whisper"
    )
    assert whisper_status["available"] is False
    assert whisper_status["metadata"]["runtime_available"] is False


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


def test_daemon_registry_skips_colliding_openai_audio_binding(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad_audio_binding = OpenAICompatibleAudioBindingConfig.model_construct(
        provider="WHISPER",
        url="http://localhost:8080/v1",
        model="whisper-large-v3",
        transcription_enabled=True,
        translation_enabled=True,
        timeout_seconds=30.0,
    )
    voice = VoiceConfig().model_copy(
        update={
            "enabled": True,
            "openai_compatible_audio": [bad_audio_binding],
        }
    )
    config = DaemonConfig().model_copy(update={"voice": voice})

    caplog.set_level("WARNING", logger="gobby.ai.registry")
    registry = build_daemon_ai_capability_registry(
        config,
        provider_installed=lambda _entry: False,
    )

    bindings = [
        binding
        for binding in registry.bindings_for(AICapability.AUDIO_TRANSCRIBE)
        if binding.provider == "whisper"
    ]
    assert len(bindings) == 1
    assert bindings[0].adapter_style == AIAdapterStyle.LOCAL
    assert (
        "Skipping duplicate OpenAI-compatible audio binding for audio_transcribe provider 'whisper'"
    ) in caplog.text


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


def test_capability_binding_probe_exception_caches_unavailable() -> None:
    calls = 0

    def unavailable_probe() -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("probe transport failed")

    binding = CapabilityBinding(
        capability=AICapability.TEXT_GENERATE,
        provider="codex",
        adapter_style=AIAdapterStyle.DAEMON,
        available=True,
        availability_probe=unavailable_probe,
        availability_probe_ttl_seconds=60.0,
    )

    assert binding.available is False
    assert binding._availability_probe_result is False
    assert binding._availability_checked_at is not None
    assert binding.available is False
    assert calls == 1


def test_daemon_registry_reprobes_provider_installation_after_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.ai.registry.PROVIDER_INSTALL_PROBE_TTL_SECONDS", 0.0)
    installed_state = {"codex": False}

    def installed(entry: ProviderMetadata) -> bool:
        return installed_state.get(entry.provider, False)

    class TextGenerationStub:
        def __init__(self, registry: AICapabilityRegistry) -> None:
            self.registry = registry

        async def generate(self, request: Any) -> str:
            raise AssertionError("not called")

        async def generate_json(self, request: Any) -> dict[str, Any]:
            raise AssertionError("not called")

    registry = build_daemon_ai_capability_registry(DaemonConfig(), provider_installed=installed)
    service = LLMService(DaemonConfig(), text_generation=TextGenerationStub(registry))
    initial_route_status = build_daemon_ai_capability_registry(
        DaemonConfig(),
        provider_installed=installed,
    ).status_snapshot()
    initial_route_binding = next(
        binding
        for binding in initial_route_status["capabilities"]["text_generate"]["bindings"]
        if binding["provider"] == "codex"
    )

    with pytest.raises(CapabilityUnavailableError):
        registry.select(AICapability.TEXT_GENERATE, provider="codex")
    assert "codex" not in service.enabled_providers
    assert initial_route_binding["available"] is False

    installed_state["codex"] = True

    assert registry.select(AICapability.TEXT_GENERATE, provider="codex").provider == "codex"
    fresh_registry = build_daemon_ai_capability_registry(
        DaemonConfig(),
        provider_installed=installed,
    )
    assert fresh_registry.select(AICapability.TEXT_GENERATE, provider="codex").provider == "codex"
    route_status = fresh_registry.status_snapshot()
    route_binding = next(
        binding
        for binding in route_status["capabilities"]["text_generate"]["bindings"]
        if binding["provider"] == "codex"
    )
    assert route_binding["available"] is True
    assert "codex" in service.enabled_providers


def test_text_bindings_drop_vision_extract_metadata() -> None:
    registry = build_daemon_ai_capability_registry(
        DaemonConfig(
            ai=AIConfig(
                generation=GenerationConfig(
                    endpoints={
                        "lm-studio": {
                            "api_base": "http://localhost:1234/v1",
                            "model": "llava",
                            "probed_model": "llava",
                            "input_modalities": ["text", "image"],
                            "probed_json": True,
                            "probed_tools": False,
                        }
                    }
                )
            ),
        ),
        provider_installed=lambda _entry: True,
    )

    text = registry.binding(AICapability.TEXT_GENERATE, "endpoint:lm-studio")
    vision = registry.binding(AICapability.VISION_EXTRACT, "endpoint:lm-studio")

    assert text is not None
    assert "vision_extract" not in text.metadata
    assert vision is not None
    assert vision.available is True
    assert "vision_extract" not in vision.metadata


def test_tool_chat_clientless_unavailable() -> None:
    config = DaemonConfig(
        ai=AIConfig(
            generation=GenerationConfig(
                endpoints={
                    "lm-studio": {
                        "protocol": "lmstudio",
                        "api_base": "http://localhost:1234/v1",
                        "model": "gemma",
                        "tool_chat": True,
                    },
                    "ollama": {
                        "protocol": "ollama",
                        "api_base": "http://localhost:11434/v1",
                        "model": "llama3",
                        "tool_chat": True,
                    },
                    "vllm-local": {
                        "protocol": "vllm",
                        "api_base": "http://localhost:8000/v1",
                        "model": "qwen2.5-vl",
                        "tool_chat": True,
                    },
                }
            )
        ),
    )
    registry = build_daemon_ai_capability_registry(
        config,
        provider_installed=lambda _entry: True,
    )

    for name in ("lm-studio", "ollama"):
        binding = registry.binding(AICapability.TOOL_CHAT, f"endpoint:{name}")
        assert binding is not None
        assert binding.available is False
        assert binding.reason is not None
        with pytest.raises(CapabilityUnavailableError, match="unavailable"):
            registry.select(AICapability.TOOL_CHAT, provider=f"endpoint:{name}")

    vllm = registry.binding(AICapability.TOOL_CHAT, "endpoint:vllm-local")
    assert vllm is not None
    assert vllm.available is True
    client = _local_client_factory(config)(vllm)
    assert client is not None
    assert getattr(client, "chat", None) is not None


def test_tool_binding_probe_evidence_gate() -> None:
    config = DaemonConfig(
        ai=AIConfig(
            generation=GenerationConfig(
                endpoints={
                    "vllm-failed": {
                        "protocol": "vllm",
                        "api_base": "http://localhost:8000/v1",
                        "model": "qwen2.5-vl",
                        "tool_chat": True,
                        "probed_tools": False,
                    },
                    "vllm-fresh": {
                        "protocol": "vllm",
                        "api_base": "http://localhost:8001/v1",
                        "model": "qwen2.5-vl",
                        "tool_chat": True,
                    },
                    "lm-studio": {
                        "protocol": "lmstudio",
                        "api_base": "http://localhost:1234/v1",
                        "model": "gemma",
                        "tool_chat": True,
                    },
                }
            )
        ),
    )
    failed_endpoint = config.ai.generation.endpoints["vllm-failed"]
    assert failed_endpoint.tool_chat is True
    assert failed_endpoint.probed_tools is False

    registry = build_daemon_ai_capability_registry(
        config,
        provider_installed=lambda _entry: True,
    )

    failed = registry.binding(AICapability.TOOL_CHAT, "endpoint:vllm-failed")
    assert failed is not None
    assert failed.available is False
    assert failed.reason is not None
    assert "probe" in failed.reason.lower()
    assert "--enable-auto-tool-choice" in failed.reason
    assert "--tool-call-parser" in failed.reason
    assert failed_endpoint.tool_chat is True
    assert failed_endpoint.probed_tools is False

    fresh = registry.binding(AICapability.TOOL_CHAT, "endpoint:vllm-fresh")
    assert fresh is not None
    assert fresh.available is True

    lm_studio = registry.binding(AICapability.TOOL_CHAT, "endpoint:lm-studio")
    assert lm_studio is not None
    assert lm_studio.available is False
    assert lm_studio.reason is not None
    assert "probe" not in lm_studio.reason.lower()
    assert "--enable-auto-tool-choice" not in lm_studio.reason
