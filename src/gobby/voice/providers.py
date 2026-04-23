"""Provider registry helpers for TTS backends."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from gobby.voice.tts import TTSProvider, TTSProviderCapabilities, TTSProviderStatus

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig

ProviderFactory = Callable[["VoiceConfig"], TTSProvider]
logger = logging.getLogger(__name__)

_PROVIDER_CLASSES: dict[str, tuple[str, str]] = {
    "chatterbox": ("gobby.voice.tts_chatterbox", "ChatterboxTurboProvider"),
}


def list_tts_providers() -> tuple[str, ...]:
    """Return the registered provider ids."""
    return tuple(sorted(_PROVIDER_CLASSES))


def _load_provider_factory(provider: str) -> ProviderFactory | None:
    spec = _PROVIDER_CLASSES.get(provider)
    if spec is None:
        return None

    module_name, class_name = spec
    try:
        module = importlib.import_module(module_name)
        provider_cls = cast(ProviderFactory, getattr(module, class_name))
    except (ImportError, AttributeError):
        logger.debug(
            "Failed to load TTS provider factory %s from %s.%s",
            provider,
            module_name,
            class_name,
            exc_info=True,
        )
        return None
    return provider_cls


def create_tts_provider(config: VoiceConfig) -> TTSProvider | None:
    """Instantiate the configured TTS provider."""
    provider_name = getattr(config, "tts_provider", "chatterbox")
    factory = _load_provider_factory(provider_name)
    if factory is None:
        return None
    try:
        return factory(config)
    except Exception:
        logger.warning("Failed to initialize TTS provider %s", provider_name, exc_info=True)
        return None


def get_tts_provider_status(config: VoiceConfig) -> TTSProviderStatus:
    """Return provider status without making callers branch on provider type."""
    provider_name = getattr(config, "tts_provider", "chatterbox")
    provider = create_tts_provider(config)
    if provider is None:
        reason = f"Unknown TTS provider: {provider_name}"
        if provider_name in _PROVIDER_CLASSES:
            reason = f"TTS provider unavailable: {provider_name}"
        return TTSProviderStatus(
            provider=provider_name,
            available=False,
            reason=reason,
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
