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
