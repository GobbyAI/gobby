"""Secret-reference handling for OpenAI-compatible voice audio bindings."""

from __future__ import annotations

from collections.abc import Callable, Collection
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from gobby.config.secret_mask import MASKED_SECRET
from gobby.storage.secret_names import SECRET_REF_PATTERN

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig

VOICE_AUDIO_BINDINGS_KEY = "voice.openai_compatible_audio"


class VoiceSecretResolutionError(ValueError):
    """A configured voice credential reference has no resolvable payload."""

    def __init__(self, secret_name: str) -> None:
        self.secret_name = secret_name
        super().__init__(f"Voice API key secret {secret_name!r} is unavailable")


def is_secret_reference(value: object) -> bool:
    """Return whether value is exactly one supported secret reference."""
    return isinstance(value, str) and SECRET_REF_PATTERN.fullmatch(value) is not None


def _bindings(config: dict[str, Any]) -> object | None:
    if VOICE_AUDIO_BINDINGS_KEY in config:
        flat_bindings: object = config[VOICE_AUDIO_BINDINGS_KEY]
        return flat_bindings
    voice = config.get("voice")
    if isinstance(voice, dict):
        nested_bindings: object | None = voice.get("openai_compatible_audio")
        return nested_bindings
    return None


def validate_structured_references(
    key: str,
    value: object,
    reference_fields: Collection[str],
) -> None:
    """Reject plaintext values in registry-declared structured secret fields."""
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        for field in reference_fields:
            field_value = item.get(field)
            if field_value in (None, "") or is_secret_reference(field_value):
                continue
            raise ValueError(
                f"{key}[{index}].{field} must use a $secret:NAME reference; "
                "plaintext values cannot be persisted"
            )


def restore_masked_structured_references(
    key: str,
    value: object,
    persisted_value: object,
    reference_fields: Collection[str],
    identity_field: str,
) -> object:
    """Restore masked structured fields from persisted secret references."""
    restored = deepcopy(value)
    if not isinstance(restored, list):
        raise ValueError(f"{key} must be a list")

    references: dict[tuple[str, str], str] = {}
    persisted_identities: set[str] = set()
    if isinstance(persisted_value, list):
        for index, item in enumerate(persisted_value):
            if not isinstance(item, dict):
                continue
            identity = _structured_identity(key, index, item, identity_field)
            if identity in persisted_identities:
                continue
            persisted_identities.add(identity)
            for field in reference_fields:
                field_value = item.get(field)
                if isinstance(field_value, str) and is_secret_reference(field_value):
                    references[(identity, field)] = field_value

    incoming_identities: set[str] = set()
    for index, item in enumerate(restored):
        if not isinstance(item, dict):
            continue
        identity = _structured_identity(key, index, item, identity_field)
        if identity in incoming_identities:
            raise ValueError(f"{key} incoming items have duplicate {identity_field} {identity!r}")
        incoming_identities.add(identity)

    for index, item in enumerate(restored):
        if not isinstance(item, dict):
            continue
        identity = _structured_identity(key, index, item, identity_field)
        for field in reference_fields:
            if item.get(field) != MASKED_SECRET:
                continue
            reference = references.get((identity, field))
            if reference is None:
                raise ValueError(
                    f"{key}[{index}].{field} is masked but no persisted secret reference "
                    "exists; provide $secret:NAME"
                )
            item[field] = reference

    return restored


def _structured_identity(
    key: str,
    index: int,
    item: dict[str, Any],
    identity_field: str,
) -> str:
    identity = item.get(identity_field)
    if not isinstance(identity, str) or not identity:
        raise ValueError(f"{key}[{index}].{identity_field} must be a non-empty string")
    return identity


def mask_structured_references(
    value: object,
    reference_fields: Collection[str],
) -> object:
    """Return a copy with registry-declared structured secret fields masked."""
    masked = deepcopy(value)
    if not isinstance(masked, list):
        return masked
    for item in masked:
        if not isinstance(item, dict):
            continue
        for field in reference_fields:
            if item.get(field) not in (None, ""):
                item[field] = MASKED_SECRET
    return masked


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
        binding["api_key"] = MASKED_SECRET
    return masked


def resolve_voice_binding_api_keys(
    config: DaemonConfig,
    secret_resolver: Callable[[str], str | None],
) -> DaemonConfig:
    """Return a typed-config copy with binding API-key references resolved."""
    bindings = config.voice.openai_compatible_audio
    if not any(is_secret_reference(binding.api_key) for binding in bindings):
        return config
    resolved_config = config.model_copy(deep=True)
    for binding in resolved_config.voice.openai_compatible_audio:
        value = binding.api_key
        if not isinstance(value, str):
            continue
        match = SECRET_REF_PATTERN.fullmatch(value)
        if match is None:
            continue
        secret_name = match.group(1)
        plaintext = secret_resolver(secret_name)
        if plaintext is None:
            raise VoiceSecretResolutionError(secret_name)
        binding.api_key = plaintext
    return resolved_config
