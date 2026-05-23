"""Tests for MemoryConfig, QdrantConfig, FalkorConfig, and EmbeddingsConfig fields."""

from __future__ import annotations

import pytest

from gobby.config.persistence import EmbeddingsConfig, MemoryConfig, QdrantConfig
from gobby.storage.config_store import config_key_to_secret_name

pytestmark = pytest.mark.unit


# ===========================================================================
# QdrantConfig tests (fields moved from MemoryConfig)
# ===========================================================================


def test_qdrant_url_defaults_to_localhost() -> None:
    """QdrantConfig.url should default to http://localhost:6333."""
    config = QdrantConfig()
    assert config.url == "http://localhost:6333"


def test_qdrant_api_key_defaults_to_none() -> None:
    """QdrantConfig.api_key should default to None."""
    config = QdrantConfig()
    assert config.api_key is None


def test_qdrant_url_accepted() -> None:
    """QdrantConfig.url should accept a URL string."""
    config = QdrantConfig(url="http://localhost:6333")
    assert config.url == "http://localhost:6333"


def test_qdrant_api_key_accepts_env_var_syntax() -> None:
    """QdrantConfig.api_key should accept ${ENV_VAR} syntax."""
    config = QdrantConfig(api_key="${qdrant_api_key}")
    assert config.api_key == "${qdrant_api_key}"


# ===========================================================================
# MemoryConfig tests (remaining fields only)
# ===========================================================================


def test_removed_fields_no_longer_exist() -> None:
    """Legacy fields should not exist as attributes on MemoryConfig."""
    config = MemoryConfig()
    removed_fields = [
        "search_backend",
        "embedding_weight",
        "keyword_weight",
        "importance_threshold",
        "decay_enabled",
        "decay_rate",
        "decay_floor",
        "qdrant_path",
        "qdrant_url",
        "qdrant_api_key",
        "embedding_model",
        "embedding_api_base",
        "embedding_api_key",
        "embedding_dim",
        "neo4j_url",
        "neo4j_auth",
        "neo4j_database",
        "kg_provider",
        "kg_model",
    ]
    for field_name in removed_fields:
        assert not hasattr(config, field_name), f"{field_name} should be removed"


def test_kept_fields_still_exist() -> None:
    """Fields that should be kept must still exist."""
    config = MemoryConfig()
    assert hasattr(config, "enabled")
    assert hasattr(config, "backend")
    assert hasattr(config, "auto_crossref")
    assert hasattr(config, "crossref_threshold")
    assert hasattr(config, "crossref_max_links")
    assert hasattr(config, "access_debounce_seconds")
    assert hasattr(config, "kg")
    assert hasattr(config, "code_link_min_score")


def test_old_config_with_removed_fields_does_not_crash() -> None:
    """Config YAML with old fields should not crash (gracefully ignored)."""
    # Simulate loading old config with removed fields
    config = MemoryConfig.model_validate(
        {
            "enabled": True,
            "search_backend": "auto",
            "embedding_weight": 0.6,
            "keyword_weight": 0.4,
            "importance_threshold": 0.7,
            "decay_enabled": True,
            "decay_rate": 0.05,
            "decay_floor": 0.1,
        }
    )
    assert config.enabled is True
    # Removed fields should be silently ignored
    assert not hasattr(config, "search_backend")


# ===========================================================================
# FalkorConfig tests (fields moved from MemoryConfig)
# ===========================================================================


def test_falkor_host_defaults_to_loopback() -> None:
    """FalkorConfig.host should default to the Docker host mapping."""
    from gobby.config.persistence import FalkorConfig

    config = FalkorConfig()
    assert config.host == "127.0.0.1"


def test_falkor_port_defaults_to_redis_mapping() -> None:
    """FalkorConfig.port should default to the host-side Redis port."""
    from gobby.config.persistence import FalkorConfig

    config = FalkorConfig()
    assert config.port == 16379


def test_falkor_requirepass_defaults_to_none() -> None:
    """FalkorConfig.requirepass should default to None until configured."""
    from gobby.config.persistence import FalkorConfig

    config = FalkorConfig()
    assert config.requirepass is None


def test_databases_config_exposes_falkordb_field() -> None:
    """DatabasesConfig should expose databases.falkordb."""
    from gobby.config.persistence import DatabasesConfig, FalkorConfig

    config = DatabasesConfig()
    assert isinstance(config.falkordb, FalkorConfig)
    assert not hasattr(config, "neo4j")


def test_falkordb_requirepass_secret_name_is_unique() -> None:
    """databases.falkordb.requirepass should derive the requirepass secret name."""
    assert config_key_to_secret_name("databases.falkordb.requirepass") == "requirepass"


# ===========================================================================
# EmbeddingsConfig tests (fields moved from MemoryConfig)
# ===========================================================================


def test_embedding_model_default() -> None:
    """EmbeddingsConfig.model should default to nomic-embed-text."""
    config = EmbeddingsConfig()
    assert config.model == "nomic-embed-text"


def test_embedding_dim_default() -> None:
    """EmbeddingsConfig.dim should default to 768."""
    config = EmbeddingsConfig()
    assert config.dim == 768
