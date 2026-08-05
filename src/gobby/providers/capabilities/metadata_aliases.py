"""ConfigStore-backed provider identity aliases for OpenRouter metadata."""

from __future__ import annotations

from typing import Protocol

from gobby.config.ai import (
    ModelMetadataAlias,
    model_metadata_alias_source_key,
    parse_model_metadata_aliases,
)

MODEL_METADATA_ALIASES_KEY = "ai.model_metadata_aliases"

DEFAULT_MODEL_METADATA_ALIASES: tuple[ModelMetadataAlias, ...] = (
    ModelMetadataAlias(
        provider="droid",
        provider_model_id="claude-haiku-4-5-20251001",
        openrouter_model_id="anthropic/claude-haiku-4.5",
    ),
    ModelMetadataAlias(
        provider="droid",
        provider_model_id="deepseek-v4-pro",
        openrouter_model_id="deepseek/deepseek-v4-pro",
    ),
    ModelMetadataAlias(
        provider="droid",
        provider_model_id="grok-4.5",
        openrouter_model_id="x-ai/grok-4.5",
    ),
    ModelMetadataAlias(
        provider="droid",
        provider_model_id="nemotron-3-ultra",
        openrouter_model_id="nvidia/nemotron-3-ultra-550b-a55b",
    ),
)


class AliasConfigReader(Protocol):
    def get(self, key: str) -> object | None: ...


class AliasConfigStore(AliasConfigReader, Protocol):
    def list_keys(self, prefix: str | None = None) -> list[str]: ...

    def set(self, key: str, value: object, source: str = "user") -> None: ...


def load_model_metadata_aliases(store: AliasConfigReader) -> tuple[ModelMetadataAlias, ...]:
    """Read and validate the current alias configuration."""
    return parse_model_metadata_aliases(store.get(MODEL_METADATA_ALIASES_KEY))


def find_model_metadata_alias(
    store: AliasConfigReader,
    provider: str,
    provider_model_id: str,
) -> ModelMetadataAlias | None:
    """Find an exact provider-scoped alias from the current ConfigStore value."""
    source_key = model_metadata_alias_source_key(provider, provider_model_id)
    return next(
        (
            alias
            for alias in load_model_metadata_aliases(store)
            if (alias.provider, alias.provider_model_id) == source_key
        ),
        None,
    )


def seed_model_metadata_aliases(store: AliasConfigStore) -> bool:
    """Install bundled aliases only when the ConfigStore key is absent."""
    if MODEL_METADATA_ALIASES_KEY in store.list_keys(prefix=MODEL_METADATA_ALIASES_KEY):
        return False
    store.set(
        MODEL_METADATA_ALIASES_KEY,
        [alias.model_dump(mode="json") for alias in DEFAULT_MODEL_METADATA_ALIASES],
        source="default",
    )
    return True
