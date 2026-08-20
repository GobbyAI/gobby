"""Provider registry helpers for TTS backends."""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING, cast

from gobby.voice.tts import TTSProvider, TTSProviderCapabilities, TTSProviderStatus

if TYPE_CHECKING:
    from gobby.config.voice import VoiceConfig

ProviderFactory = Callable[["VoiceConfig"], TTSProvider]
logger = logging.getLogger(__name__)

_PROVIDER_CLASSES: dict[str, tuple[str, str]] = {
    "chatterbox": ("gobby.voice.tts_chatterbox", "ChatterboxTurboProvider"),
}


def _load_provider_factory(provider: str) -> tuple[ProviderFactory | None, str | None]:
    spec = _PROVIDER_CLASSES.get(provider)
    if spec is None:
        return None, f"Unknown TTS provider: {provider}"

    module_name, class_name = spec
    try:
        module = import_module(module_name)
    except ImportError:
        logger.debug(
            "Failed to import TTS provider module %s for %s",
            module_name,
            provider,
            exc_info=True,
        )
        return None, f"TTS provider module import failed: {module_name}"

    try:
        provider_cls = cast(ProviderFactory, getattr(module, class_name))
    except AttributeError:
        logger.debug(
            "TTS provider factory %s is missing from %s",
            class_name,
            module_name,
            exc_info=True,
        )
        return None, f"TTS provider class missing: {module_name}.{class_name}"
    return provider_cls, None


def _create_tts_provider(config: VoiceConfig) -> tuple[TTSProvider | None, str | None]:
    provider_name = getattr(config, "tts_provider", "chatterbox")
    factory, failure_reason = _load_provider_factory(provider_name)
    if factory is None:
        return None, failure_reason
    try:
        return factory(config), None
    except Exception:
        logger.warning("Failed to initialize TTS provider %s", provider_name, exc_info=True)
        return None, f"TTS provider unavailable: {provider_name}"


def create_tts_provider(config: VoiceConfig) -> TTSProvider | None:
    """Instantiate the configured TTS provider."""
    provider, _ = _create_tts_provider(config)
    return provider


def get_tts_provider_status(config: VoiceConfig) -> TTSProviderStatus:
    """Return provider status without making callers branch on provider type."""
    provider_name = getattr(config, "tts_provider", "chatterbox")
    provider, failure_reason = _create_tts_provider(config)
    if provider is None:
        return TTSProviderStatus(
            provider=provider_name,
            available=False,
            reason=failure_reason or f"TTS provider unavailable: {provider_name}",
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
