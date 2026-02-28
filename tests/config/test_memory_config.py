"""Tests for MemoryConfig Qdrant fields and removed legacy fields."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gobby.config.persistence import MemoryConfig

pytestmark = pytest.mark.unit


def test_qdrant_path_defaults_to_none() -> None:
    """qdrant_path should default to None."""
    config = MemoryConfig()
    assert config.qdrant_path is None


def test_qdrant_url_defaults_to_none() -> None:
    """qdrant_url should default to None."""
    config = MemoryConfig()
    assert config.qdrant_url is None


def test_qdrant_api_key_defaults_to_none() -> None:
    """qdrant_api_key should default to None."""
    config = MemoryConfig()
    assert config.qdrant_api_key is None


def test_qdrant_path_accepted() -> None:
    """qdrant_path should accept a string path."""
    config = MemoryConfig(qdrant_path="/tmp/qdrant")
    assert config.qdrant_path == "/tmp/qdrant"


def test_qdrant_url_accepted() -> None:
    """qdrant_url should accept a URL string."""
    config = MemoryConfig(qdrant_url="http://localhost:6333")
    assert config.qdrant_url == "http://localhost:6333"


def test_qdrant_api_key_accepts_env_var_syntax() -> None:
    """qdrant_api_key should accept ${ENV_VAR} syntax."""
    config = MemoryConfig(qdrant_api_key="${qdrant_api_key}")
    assert config.qdrant_api_key == "${qdrant_api_key}"


def test_qdrant_path_and_url_mutual_exclusivity() -> None:
    """Setting both qdrant_path and qdrant_url should raise an error."""
    with pytest.raises(ValidationError, match="mutually exclusive"):
        MemoryConfig(qdrant_path="/tmp/qdrant", qdrant_url="http://localhost:6333")


def test_removed_fields_no_longer_exist() -> None:
    """Legacy fields should not exist as attributes on MemoryConfig."""
    config = MemoryConfig()
    removed_fields = [
        "search_backend",
        "embedding_weight",
        "tfidf_weight",
        "importance_threshold",
        "decay_enabled",
        "decay_rate",
        "decay_floor",
    ]
    for field_name in removed_fields:
        assert not hasattr(config, field_name), f"{field_name} should be removed"


def test_kept_fields_still_exist() -> None:
    """Fields that should be kept must still exist."""
    config = MemoryConfig()
    assert hasattr(config, "enabled")
    assert hasattr(config, "backend")
    assert hasattr(config, "embedding_model")
    assert hasattr(config, "auto_crossref")
    assert hasattr(config, "crossref_threshold")
    assert hasattr(config, "crossref_max_links")
    assert hasattr(config, "neo4j_url")
    assert hasattr(config, "neo4j_auth")
    assert hasattr(config, "neo4j_database")


def test_old_config_with_removed_fields_does_not_crash() -> None:
    """Config YAML with old fields should not crash (gracefully ignored)."""
    # Simulate loading old config with removed fields
    config = MemoryConfig.model_validate(
        {
            "enabled": True,
            "search_backend": "auto",
            "embedding_weight": 0.6,
            "tfidf_weight": 0.4,
            "importance_threshold": 0.7,
            "decay_enabled": True,
            "decay_rate": 0.05,
            "decay_floor": 0.1,
        }
    )
    assert config.enabled is True
    # Removed fields should be silently ignored
    assert not hasattr(config, "search_backend")
