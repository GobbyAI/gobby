from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "gobby"
ALLOWED_LITERAL_FILES = {
    SRC_ROOT / "config" / "embedding_keys.py",
}
EMBEDDING_CONFIG_KEY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])(?:ai\.)?embeddings\."
    r"(?:api_base|api_key|model|dim|query_prefix)(?![A-Za-z0-9_.])"
)


@pytest.mark.slow(reason="AST guard scans every source file for forbidden literal config keys.")
def test_embedding_keys_centralized_and_guarded() -> None:
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        if path in ALLOWED_LITERAL_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if EMBEDDING_CONFIG_KEY_PATTERN.search(node.value):
                    offenders.append(f"{path.relative_to(SRC_ROOT)}:{node.lineno}")

    assert offenders == [], f"Embedding config key literals outside central module: {offenders}"


def test_provider_is_not_a_canonical_embedding_key() -> None:
    from gobby.config.embedding_keys import AI_EMBEDDING_CONFIG_KEYS

    assert "ai.embeddings.provider" not in AI_EMBEDDING_CONFIG_KEYS


def test_runtime_mapping_rejects_bare_storage_embedding_prefix() -> None:
    from gobby.config.embedding_keys import (
        AI_EMBEDDINGS_CONFIG_PREFIX,
        runtime_embedding_config_key_to_storage_key,
    )

    with pytest.raises(ValueError, match=r"ai\.embeddings"):
        runtime_embedding_config_key_to_storage_key(AI_EMBEDDINGS_CONFIG_PREFIX)


def test_embedding_storage_and_runtime_keys_round_trip_all_fields() -> None:
    from gobby.config.embedding_keys import (
        AI_EMBEDDINGS_CONFIG_PREFIX,
        EMBEDDING_CONFIG_FIELDS,
        RUNTIME_EMBEDDINGS_CONFIG_PREFIX,
        canonical_embedding_key,
        runtime_embedding_config_key_to_storage_key,
        runtime_embedding_key,
        storage_embedding_config_key_to_runtime_key,
    )

    assert (
        storage_embedding_config_key_to_runtime_key(AI_EMBEDDINGS_CONFIG_PREFIX)
        == RUNTIME_EMBEDDINGS_CONFIG_PREFIX
    )
    assert (
        runtime_embedding_config_key_to_storage_key(RUNTIME_EMBEDDINGS_CONFIG_PREFIX)
        == AI_EMBEDDINGS_CONFIG_PREFIX
    )

    for field in EMBEDDING_CONFIG_FIELDS:
        storage_key = canonical_embedding_key(field)
        runtime_key = runtime_embedding_key(field)

        assert storage_embedding_config_key_to_runtime_key(storage_key) == runtime_key
        assert runtime_embedding_config_key_to_storage_key(runtime_key) == storage_key
        assert runtime_embedding_config_key_to_storage_key(storage_key) == storage_key


def test_embedding_entry_mapping_preserves_empty_dicts_and_none_values() -> None:
    from gobby.config.embedding_keys import (
        AI_EMBEDDING_API_KEY_KEY,
        runtime_embedding_config_entries_to_storage,
        storage_embedding_config_entries_to_runtime,
    )

    assert storage_embedding_config_entries_to_runtime({}) == {}
    assert runtime_embedding_config_entries_to_storage({}) == {}

    storage_entries = {AI_EMBEDDING_API_KEY_KEY: None, "feature.enabled": None}
    runtime_entries = {"embeddings.api_key": None, "feature.enabled": None}

    assert storage_embedding_config_entries_to_runtime(storage_entries) == runtime_entries
    assert runtime_embedding_config_entries_to_storage(runtime_entries) == storage_entries


@pytest.mark.parametrize(
    "key",
    [
        "service.enabled",
        "embeddings",
        "embeddings.model",
        "embeddings.provider",
    ],
)
def test_storage_to_runtime_preserves_non_storage_embedding_keys(key: str) -> None:
    from gobby.config.embedding_keys import storage_embedding_config_key_to_runtime_key

    assert storage_embedding_config_key_to_runtime_key(key) == key


@pytest.mark.parametrize(
    "key",
    [
        "ai.embeddings.provider",
        "ai.embeddings.unknown",
        "ai.embeddings.model.suffix",
    ],
)
def test_storage_to_runtime_rejects_invalid_storage_embedding_keys(key: str) -> None:
    from gobby.config.embedding_keys import (
        storage_embedding_config_entries_to_runtime,
        storage_embedding_config_key_to_runtime_key,
    )

    with pytest.raises(ValueError, match=r"(?:Embedding|Unsupported embedding) .*config key"):
        storage_embedding_config_key_to_runtime_key(key)

    with pytest.raises(ValueError, match=r"(?:Embedding|Unsupported embedding) .*config key"):
        storage_embedding_config_entries_to_runtime({key: "value"})


@pytest.mark.parametrize(
    "literal",
    [
        "ai.embeddings.api_base",
        "ai.embeddings.api_key",
        "ai.embeddings.model",
        "ai.embeddings.dim",
        "ai.embeddings.query_prefix",
        "embeddings.model",
        "prefix ai.embeddings.model suffix",
        "'ai.embeddings.model'",
    ],
)
def test_embedding_config_key_pattern_matches_boundaries(literal: str) -> None:
    assert EMBEDDING_CONFIG_KEY_PATTERN.search(literal)


@pytest.mark.parametrize(
    "literal",
    [
        "xai.embeddings.model",
        "x.ai.embeddings.model",
        "x.embeddings.model",
        "ai.embeddings.model_suffix",
        "ai.embeddings.model.suffix",
        "ai.embeddings.provider",
        "embedding.model",
        "embeddings.model_name",
    ],
)
def test_embedding_config_key_pattern_rejects_embedded_text(literal: str) -> None:
    assert EMBEDDING_CONFIG_KEY_PATTERN.search(literal) is None
