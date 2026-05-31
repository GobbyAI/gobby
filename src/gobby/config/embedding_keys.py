"""Canonical config keys for shared embedding settings."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

AI_CONFIG_SECTION = "ai"
RUNTIME_EMBEDDINGS_CONFIG_SECTION = "embeddings"

EMBEDDING_MODEL_FIELD = "model"
EMBEDDING_API_BASE_FIELD = "api_base"
EMBEDDING_API_KEY_FIELD = "api_key"
EMBEDDING_DIM_FIELD = "dim"
EMBEDDING_QUERY_PREFIX_FIELD = "query_prefix"
_REMOVED_EMBEDDING_PROVIDER_FIELD = "provider"

EMBEDDING_CONFIG_FIELDS = (
    EMBEDDING_API_BASE_FIELD,
    EMBEDDING_MODEL_FIELD,
    EMBEDDING_API_KEY_FIELD,
    EMBEDDING_QUERY_PREFIX_FIELD,
    EMBEDDING_DIM_FIELD,
)

AI_EMBEDDINGS_CONFIG_PREFIX = f"{AI_CONFIG_SECTION}.{RUNTIME_EMBEDDINGS_CONFIG_SECTION}"
RUNTIME_EMBEDDINGS_CONFIG_PREFIX = RUNTIME_EMBEDDINGS_CONFIG_SECTION

AI_EMBEDDING_API_BASE_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_API_BASE_FIELD}"
AI_EMBEDDING_MODEL_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_MODEL_FIELD}"
AI_EMBEDDING_API_KEY_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_API_KEY_FIELD}"
AI_EMBEDDING_DIM_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_DIM_FIELD}"
AI_EMBEDDING_QUERY_PREFIX_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_QUERY_PREFIX_FIELD}"

AI_EMBEDDING_CONFIG_KEYS = (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_MODEL_KEY,
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_QUERY_PREFIX_KEY,
)

EMBEDDING_API_KEY_SECRET_NAME = "embeddings_api_key"
EMBEDDING_API_KEY_SECRET_REF = f"$secret:{EMBEDDING_API_KEY_SECRET_NAME}"
_AI_EMBEDDING_PROVIDER_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{_REMOVED_EMBEDDING_PROVIDER_FIELD}"
_RUNTIME_EMBEDDING_PROVIDER_KEY = (
    f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}.{_REMOVED_EMBEDDING_PROVIDER_FIELD}"
)
_ALLOWED_FIELDS_TEXT = ", ".join(EMBEDDING_CONFIG_FIELDS)


def canonical_embedding_key(field: str) -> str:
    return f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{field}"


def runtime_embedding_key(field: str) -> str:
    return f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}.{field}"


def is_runtime_embedding_config_key(key: str) -> bool:
    return key in {runtime_embedding_key(field) for field in EMBEDDING_CONFIG_FIELDS}


def is_ai_embedding_config_key(key: str) -> bool:
    return key in {canonical_embedding_key(field) for field in EMBEDDING_CONFIG_FIELDS}


def is_removed_embedding_config_store_key(key: str) -> bool:
    """Return true for persisted embedding keys that should be deleted at load/migration time."""
    return (
        key == RUNTIME_EMBEDDINGS_CONFIG_PREFIX
        or key.startswith(f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}.")
        or key == _AI_EMBEDDING_PROVIDER_KEY
    )


def _embedding_key_error(key: str) -> str:
    if key == _AI_EMBEDDING_PROVIDER_KEY or key == _RUNTIME_EMBEDDING_PROVIDER_KEY:
        return (
            f"Embedding provider config key '{key}' has been removed. "
            "Provider is inferred from ai.embeddings.api_base, ai.embeddings.model, "
            "and ai.embeddings.api_key."
        )
    if key == RUNTIME_EMBEDDINGS_CONFIG_PREFIX or key.startswith(
        f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}."
    ):
        field = key.removeprefix(f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}.")
        replacement = (
            AI_EMBEDDINGS_CONFIG_PREFIX
            if key == RUNTIME_EMBEDDINGS_CONFIG_PREFIX
            else canonical_embedding_key(field)
        )
        return (
            f"Embedding config alias '{key}' has been removed. Use canonical key '{replacement}'."
        )
    return (
        f"Unsupported embedding config key '{key}'. "
        f"Allowed canonical keys are ai.embeddings.{{{_ALLOWED_FIELDS_TEXT}}}."
    )


def validate_embedding_storage_config_key(key: str) -> None:
    """Validate a key before writing it directly to config_store."""
    if key == RUNTIME_EMBEDDINGS_CONFIG_PREFIX or key.startswith(
        f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}."
    ):
        raise ValueError(_embedding_key_error(key))
    if key == AI_EMBEDDINGS_CONFIG_PREFIX:
        raise ValueError(_embedding_key_error(key))
    if key.startswith(f"{AI_EMBEDDINGS_CONFIG_PREFIX}.") and not is_ai_embedding_config_key(key):
        raise ValueError(_embedding_key_error(key))


def runtime_embedding_config_key_to_storage_key(key: str) -> str:
    """Map a runtime ``DaemonConfig.embeddings`` key to canonical config_store storage."""
    if key == RUNTIME_EMBEDDINGS_CONFIG_PREFIX:
        return AI_EMBEDDINGS_CONFIG_PREFIX
    if key.startswith(f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}."):
        field = key.removeprefix(f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}.")
        if field not in EMBEDDING_CONFIG_FIELDS:
            raise ValueError(_embedding_key_error(key))
        return canonical_embedding_key(field)
    if key == AI_EMBEDDINGS_CONFIG_PREFIX:
        return key
    if key.startswith(f"{AI_EMBEDDINGS_CONFIG_PREFIX}."):
        validate_embedding_storage_config_key(key)
    return key


def storage_embedding_config_key_to_runtime_key(key: str) -> str:
    """Map canonical config_store embedding keys to runtime ``DaemonConfig.embeddings`` keys."""
    if key == AI_EMBEDDINGS_CONFIG_PREFIX:
        return RUNTIME_EMBEDDINGS_CONFIG_PREFIX
    if not is_ai_embedding_config_key(key):
        return key
    field = key.removeprefix(f"{AI_EMBEDDINGS_CONFIG_PREFIX}.")
    return runtime_embedding_key(field)


def external_embedding_config_key_to_runtime_key(key: str) -> str:
    """Validate a public config-tool key and map canonical embedding keys to runtime keys."""
    if key == RUNTIME_EMBEDDINGS_CONFIG_PREFIX or key.startswith(
        f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}."
    ):
        raise ValueError(_embedding_key_error(key))
    if key == AI_EMBEDDINGS_CONFIG_PREFIX:
        return RUNTIME_EMBEDDINGS_CONFIG_PREFIX
    validate_embedding_storage_config_key(key)
    return storage_embedding_config_key_to_runtime_key(key)


def runtime_embedding_config_entries_to_storage(entries: Mapping[str, Any]) -> dict[str, Any]:
    return {
        runtime_embedding_config_key_to_storage_key(key): value for key, value in entries.items()
    }


def runtime_embedding_config_keys_to_storage(keys: Iterable[str]) -> set[str]:
    return {runtime_embedding_config_key_to_storage_key(key) for key in keys}


def storage_embedding_config_entries_to_runtime(entries: Mapping[str, Any]) -> dict[str, Any]:
    return {
        storage_embedding_config_key_to_runtime_key(key): value for key, value in entries.items()
    }


def embedding_config_secret_name(key: str) -> str | None:
    if key == AI_EMBEDDING_API_KEY_KEY:
        return EMBEDDING_API_KEY_SECRET_NAME
    return None
