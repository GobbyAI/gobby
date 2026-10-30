"""Every embedding fallback default matches the canonical EmbeddingsConfig default."""

import inspect
from collections.abc import Callable
from dataclasses import fields
from typing import Any

import pytest

from gobby.ai.embeddings import (
    EmbeddingService,
    _generate_embedding,
    _generate_embeddings,
    _is_embedding_configured,
    _is_embedding_reachable,
)
from gobby.config.persistence import EmbeddingsConfig
from gobby.mcp_proxy.semantic_search import SemanticToolSearch
from gobby.search.backends.embedding import EmbeddingBackend

pytestmark = pytest.mark.unit

CANONICAL_MODEL = "qwen3-embedding:0.6b-q8_0"
CANONICAL_DIM = 1024


def _default(function: Callable[..., Any], parameter: str) -> Any:
    return inspect.signature(function).parameters[parameter].default


def test_embedding_fallback_defaults_match_canonical_config_default() -> None:
    config = EmbeddingsConfig()
    observed_models = {
        "EmbeddingsConfig.model": config.model,
        "_generate_embeddings": _default(_generate_embeddings, "model"),
        "_generate_embedding": _default(_generate_embedding, "model"),
        "_is_embedding_configured": _default(_is_embedding_configured, "model"),
        "_is_embedding_reachable": _default(_is_embedding_reachable, "model"),
        "EmbeddingService.model": {f.name: f.default for f in fields(EmbeddingService)}["model"],
        "EmbeddingBackend.model": _default(EmbeddingBackend.__init__, "model"),
        "SemanticToolSearch.embedding_model": _default(
            SemanticToolSearch.__init__, "embedding_model"
        ),
    }
    observed_dims = {
        "EmbeddingsConfig.dim": config.dim,
        "SemanticToolSearch.embedding_dim": _default(SemanticToolSearch.__init__, "embedding_dim"),
    }

    mismatched = {
        name: value for name, value in observed_models.items() if value != CANONICAL_MODEL
    } | {name: value for name, value in observed_dims.items() if value != CANONICAL_DIM}
    assert mismatched == {}
