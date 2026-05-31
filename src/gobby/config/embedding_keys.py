"""Canonical config keys for shared embedding settings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

AI_CONFIG_SECTION = "ai"
RUNTIME_EMBEDDINGS_CONFIG_SECTION = "embeddings"

EMBEDDING_MODEL_FIELD = "model"
EMBEDDING_API_BASE_FIELD = "api_base"
EMBEDDING_API_KEY_FIELD = "api_key"
EMBEDDING_DIM_FIELD = "dim"
EMBEDDING_QUERY_PREFIX_FIELD = "query_prefix"
EMBEDDING_PROVIDER_FIELD = "provider"

EMBEDDING_CONFIG_FIELDS = (
    EMBEDDING_API_BASE_FIELD,
    EMBEDDING_MODEL_FIELD,
    EMBEDDING_API_KEY_FIELD,
    EMBEDDING_QUERY_PREFIX_FIELD,
    EMBEDDING_PROVIDER_FIELD,
    EMBEDDING_DIM_FIELD,
)

AI_EMBEDDINGS_CONFIG_PREFIX = f"{AI_CONFIG_SECTION}.{RUNTIME_EMBEDDINGS_CONFIG_SECTION}"
RUNTIME_EMBEDDINGS_CONFIG_PREFIX = RUNTIME_EMBEDDINGS_CONFIG_SECTION

AI_EMBEDDING_API_BASE_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_API_BASE_FIELD}"
AI_EMBEDDING_MODEL_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_MODEL_FIELD}"
AI_EMBEDDING_API_KEY_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_API_KEY_FIELD}"
AI_EMBEDDING_DIM_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_DIM_FIELD}"
AI_EMBEDDING_QUERY_PREFIX_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_QUERY_PREFIX_FIELD}"
AI_EMBEDDING_PROVIDER_KEY = f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{EMBEDDING_PROVIDER_FIELD}"

AI_EMBEDDING_CONFIG_KEYS = (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_MODEL_KEY,
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_QUERY_PREFIX_KEY,
    AI_EMBEDDING_PROVIDER_KEY,
)

EMBEDDING_API_KEY_SECRET_NAME = "embeddings_api_key"
EMBEDDING_API_KEY_SECRET_REF = f"$secret:{EMBEDDING_API_KEY_SECRET_NAME}"


def canonical_embedding_key(field: str) -> str:
    return f"{AI_EMBEDDINGS_CONFIG_PREFIX}.{field}"


def runtime_embedding_key(field: str) -> str:
    return f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}.{field}"


def is_runtime_embedding_config_key(key: str) -> bool:
    return key in {runtime_embedding_key(field) for field in EMBEDDING_CONFIG_FIELDS}


def is_ai_embedding_config_key(key: str) -> bool:
    return key in {canonical_embedding_key(field) for field in EMBEDDING_CONFIG_FIELDS}


def canonicalize_embedding_config_key(key: str) -> str:
    if key == RUNTIME_EMBEDDINGS_CONFIG_PREFIX:
        return AI_EMBEDDINGS_CONFIG_PREFIX
    if not is_runtime_embedding_config_key(key):
        return key
    field = key.removeprefix(f"{RUNTIME_EMBEDDINGS_CONFIG_PREFIX}.")
    return canonical_embedding_key(field)


def runtimeize_embedding_config_key(key: str) -> str:
    if key == AI_EMBEDDINGS_CONFIG_PREFIX:
        return RUNTIME_EMBEDDINGS_CONFIG_PREFIX
    if not is_ai_embedding_config_key(key):
        return key
    field = key.removeprefix(f"{AI_EMBEDDINGS_CONFIG_PREFIX}.")
    return runtime_embedding_key(field)


def canonicalize_embedding_config_entries(entries: Mapping[str, Any]) -> dict[str, Any]:
    return {canonicalize_embedding_config_key(key): value for key, value in entries.items()}


def runtimeize_embedding_config_entries(entries: Mapping[str, Any]) -> dict[str, Any]:
    return {runtimeize_embedding_config_key(key): value for key, value in entries.items()}


def embedding_config_secret_name(key: str) -> str | None:
    runtime_key = runtimeize_embedding_config_key(key)
    if runtime_key == runtime_embedding_key(EMBEDDING_API_KEY_FIELD):
        return EMBEDDING_API_KEY_SECRET_NAME
    return None
