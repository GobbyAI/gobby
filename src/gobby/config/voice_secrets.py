"""Secret-reference handling for OpenAI-compatible voice audio bindings."""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

VOICE_AUDIO_BINDINGS_KEY = "voice.openai_compatible_audio"
VOICE_AUDIO_API_KEY_PATH = f"{VOICE_AUDIO_BINDINGS_KEY}[].api_key"
MASKED_VOICE_AUDIO_API_KEY = "********"

_SECRET_REFERENCE_PATTERN = re.compile(r"\$secret:([A-Za-z_][A-Za-z0-9_]*)")


def is_secret_reference(value: object) -> bool:
    """Return whether value is exactly one supported secret reference."""
    return isinstance(value, str) and _SECRET_REFERENCE_PATTERN.fullmatch(value) is not None


def _bindings(config: dict[str, Any]) -> object | None:
    if VOICE_AUDIO_BINDINGS_KEY in config:
        flat_bindings: object = config[VOICE_AUDIO_BINDINGS_KEY]
        return flat_bindings
    voice = config.get("voice")
    if isinstance(voice, dict):
        nested_bindings: object | None = voice.get("openai_compatible_audio")
        return nested_bindings
    return None


def contains_voice_audio_bindings(config: dict[str, Any]) -> bool:
    """Return whether config explicitly supplies the audio binding collection."""
    if VOICE_AUDIO_BINDINGS_KEY in config:
        return True
    voice = config.get("voice")
    return isinstance(voice, dict) and "openai_compatible_audio" in voice


def validate_voice_audio_api_key_references(config: dict[str, Any]) -> None:
    """Reject persisted audio API keys that are not explicit secret references."""
    bindings = _bindings(config)
    if not isinstance(bindings, list):
        return

    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict) or "api_key" not in binding:
            continue
        value = binding["api_key"]
        if value in (None, ""):
            continue
        if is_secret_reference(value):
            continue
        raise ValueError(
            f"{VOICE_AUDIO_BINDINGS_KEY}[{index}].api_key must use a "
            "$secret:NAME reference; plaintext API keys cannot be persisted"
        )


def restore_masked_voice_audio_api_keys(
    config: dict[str, Any],
    persisted_config: dict[str, Any],
) -> dict[str, Any]:
    """Restore masked submitted keys from previously persisted safe references."""
    restored = deepcopy(config)
    bindings = _bindings(restored)
    persisted_bindings = _bindings(persisted_config)
    if not isinstance(bindings, list):
        return restored

    references_by_provider: dict[str, str] = {}
    if isinstance(persisted_bindings, list):
        for binding in persisted_bindings:
            if not isinstance(binding, dict):
                continue
            provider = binding.get("provider")
            api_key = binding.get("api_key")
            if (
                isinstance(provider, str)
                and isinstance(api_key, str)
                and is_secret_reference(api_key)
            ):
                references_by_provider[provider] = api_key

    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict) or binding.get("api_key") != MASKED_VOICE_AUDIO_API_KEY:
            continue
        provider = binding.get("provider")
        reference = references_by_provider.get(provider) if isinstance(provider, str) else None
        if reference is None:
            raise ValueError(
                f"{VOICE_AUDIO_BINDINGS_KEY}[{index}].api_key is masked but no persisted "
                "secret reference exists; provide $secret:NAME"
            )
        binding["api_key"] = reference

    return restored


def mask_voice_audio_api_keys(
    config: dict[str, Any],
    *,
    preserve_secret_references: bool = False,
) -> dict[str, Any]:
    """Return a copy with nested audio API keys masked."""
    masked = deepcopy(config)
    bindings = _bindings(masked)
    if not isinstance(bindings, list):
        return masked

    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        value = binding.get("api_key")
        if value in (None, ""):
            continue
        if preserve_secret_references and is_secret_reference(value):
            continue
        binding["api_key"] = MASKED_VOICE_AUDIO_API_KEY
    return masked


def resolve_voice_audio_api_keys(
    config: dict[str, Any],
    secret_resolver: Callable[[str], str | None],
) -> dict[str, Any]:
    """Return a copy with exact audio API-key references resolved for runtime use."""
    resolved_config = deepcopy(config)
    bindings = _bindings(resolved_config)
    if not isinstance(bindings, list):
        return resolved_config

    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        value = binding.get("api_key")
        if not isinstance(value, str):
            continue
        match = _SECRET_REFERENCE_PATTERN.fullmatch(value)
        if match is None:
            continue
        resolved_value = secret_resolver(match.group(1))
        if resolved_value is not None:
            binding["api_key"] = resolved_value
    return resolved_config
