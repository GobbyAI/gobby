"""Provider registry helpers for TTS backends."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from gobby.voice.tts import TTSProvider, TTSProviderCapabilities, TTSProviderStatus

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig

ProviderFactory = Callable[["VoiceConfig"], TTSProvider]

_PROVIDER_CLASSES: dict[str, tuple[str, str]] = {
    "chatterbox": ("gobby.voice.tts_chatterbox", "ChatterboxTurboProvider"),
    "kokoro": ("gobby.voice.tts", "KokoroTTS"),
    "voxcpm": ("gobby.voice.tts_voxcpm", "VoxCPMProvider"),
}


def list_tts_providers() -> tuple[str, ...]:
    """Return the registered provider ids."""
    return tuple(sorted(_PROVIDER_CLASSES))


def _load_provider_factory(provider: str) -> ProviderFactory | None:
    spec = _PROVIDER_CLASSES.get(provider)
    if spec is None:
        return None

    module_name, class_name = spec
    module = importlib.import_module(module_name)
    provider_cls = cast(ProviderFactory, getattr(module, class_name))
    return provider_cls


def create_tts_provider(config: VoiceConfig) -> TTSProvider | None:
    """Instantiate the configured TTS provider."""
    factory = _load_provider_factory(getattr(config, "tts_provider", "chatterbox"))
    if factory is None:
        return None
    return factory(config)


def get_tts_provider_status(config: VoiceConfig) -> TTSProviderStatus:
    """Return provider status without making callers branch on provider type."""
    provider_name = getattr(config, "tts_provider", "chatterbox")
    provider = create_tts_provider(config)
    if provider is None:
        return TTSProviderStatus(
            provider=provider_name,
            available=False,
            reason=f"Unknown TTS provider: {provider_name}",
            capabilities=TTSProviderCapabilities(),
            details={},
        )
    return provider.get_status()


def get_tts_status_for_config(config: VoiceConfig) -> TTSProviderStatus:
    """Return the public provider status for the current voice config."""
    provider_name = getattr(config, "tts_provider", "chatterbox")
    if not config.enabled:
        return TTSProviderStatus(
            provider=provider_name,
            available=False,
            reason="Voice not enabled in config",
            capabilities=TTSProviderCapabilities(),
            details={},
        )
    if not config.tts_enabled:
        return TTSProviderStatus(
            provider=provider_name,
            available=False,
            reason="TTS disabled in config",
            capabilities=TTSProviderCapabilities(),
            details={},
        )
    return get_tts_provider_status(config)
