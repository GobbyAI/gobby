"""
Tests for config/persistence.py module.

RED PHASE: Tests initially import from persistence.py (should fail),
then will pass once memory/skill config classes are extracted from app.py.
"""

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit

# =============================================================================
# Import Tests (RED phase targets)
# =============================================================================


class TestMemoryConfigImport:
    """Test that MemoryConfig can be imported from the persistence module."""

    def test_import_from_persistence_module(self) -> None:
        """Test importing MemoryConfig from config.persistence (RED phase target)."""
        from gobby.config.persistence import MemoryConfig

        assert MemoryConfig is not None


class TestMemoryBackupConfigImport:
    """Test that MemoryBackupConfig can be imported from the persistence module."""

    def test_import_from_persistence_module(self) -> None:
        """Test importing MemoryBackupConfig from config.persistence (RED phase target)."""
        from gobby.config.persistence import MemoryBackupConfig

        assert MemoryBackupConfig is not None


# =============================================================================
# MemoryConfig Tests
# =============================================================================


class TestMemoryConfigDefaults:
    """Test MemoryConfig default values."""

    def test_default_instantiation(self) -> None:
        """Test MemoryConfig creates with all defaults."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig()
        assert config.enabled is True
        assert config.backend == "local"
        assert config.embedding_model == "text-embedding-3-small"
        assert config.qdrant_path is None
        assert config.qdrant_url is None
        assert config.access_debounce_seconds == 60
        assert config.crossref_threshold == 0.3


class TestMemoryConfigCustom:
    """Test MemoryConfig with custom values."""

    def test_disabled_memory(self) -> None:
        """Test disabling memory system."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(enabled=False)
        assert config.enabled is False

    def test_custom_backend(self) -> None:
        """Test setting custom backend."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(backend="null")
        assert config.backend == "null"

    def test_custom_qdrant_path(self) -> None:
        """Test setting custom qdrant_path."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(qdrant_path="/tmp/qdrant")
        assert config.qdrant_path == "/tmp/qdrant"


class TestMemoryConfigValidation:
    """Test MemoryConfig validation."""

    def test_crossref_threshold_range(self) -> None:
        """Test that crossref_threshold must be between 0 and 1."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(crossref_threshold=0.0)
        assert config.crossref_threshold == 0.0

        config = MemoryConfig(crossref_threshold=1.0)
        assert config.crossref_threshold == 1.0

        with pytest.raises(ValidationError):
            MemoryConfig(crossref_threshold=-0.1)

        with pytest.raises(ValidationError):
            MemoryConfig(crossref_threshold=1.1)

    def test_crossref_max_links_positive(self) -> None:
        """Test that crossref_max_links must be at least 1."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(crossref_max_links=1)
        assert config.crossref_max_links == 1

        with pytest.raises(ValidationError):
            MemoryConfig(crossref_max_links=0)


# =============================================================================
# Backend Validator Tests
# =============================================================================


class TestMemoryConfigBackendValidator:
    """Test MemoryConfig backend validation."""

    def test_backend_validator_rejects_invalid(self) -> None:
        """Test that invalid backends are rejected."""
        from gobby.config.persistence import MemoryConfig

        with pytest.raises(ValidationError) as exc_info:
            MemoryConfig(backend="invalid_backend")
        assert "invalid_backend" in str(exc_info.value).lower()

    def test_backend_sqlite_alias(self) -> None:
        """Test that 'sqlite' is accepted as alias for 'local'."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(backend="sqlite")
        assert config.backend == "local"


# =============================================================================
# MemoryBackupConfig Tests
# =============================================================================


class TestMemoryBackupConfigDefaults:
    """Test MemoryBackupConfig default values."""

    def test_default_instantiation(self) -> None:
        """Test MemoryBackupConfig creates with all defaults."""
        from pathlib import Path

        from gobby.config.persistence import MemoryBackupConfig

        config = MemoryBackupConfig()
        assert config.enabled is True
        assert config.export_debounce == 5.0
        assert config.export_path == Path(".gobby/memories.jsonl")


class TestMemoryBackupConfigCustom:
    """Test MemoryBackupConfig with custom values."""

    def test_disabled_sync(self) -> None:
        """Test disabling memory sync."""
        from gobby.config.persistence import MemoryBackupConfig

        config = MemoryBackupConfig(enabled=False)
        assert config.enabled is False

    def test_custom_debounce(self) -> None:
        """Test setting custom export debounce."""
        from gobby.config.persistence import MemoryBackupConfig

        config = MemoryBackupConfig(export_debounce=10.0)
        assert config.export_debounce == 10.0

    def test_custom_export_path(self) -> None:
        """Test setting custom export path."""
        from pathlib import Path

        from gobby.config.persistence import MemoryBackupConfig

        config = MemoryBackupConfig(export_path=Path("/custom/memories.jsonl"))
        assert config.export_path == Path("/custom/memories.jsonl")


class TestMemoryBackupConfigValidation:
    """Test MemoryBackupConfig validation."""

    def test_export_debounce_non_negative(self) -> None:
        """Test that export_debounce must be non-negative."""
        from gobby.config.persistence import MemoryBackupConfig

        # Zero is allowed
        config = MemoryBackupConfig(export_debounce=0.0)
        assert config.export_debounce == 0.0

        # Negative is not
        with pytest.raises(ValidationError) as exc_info:
            MemoryBackupConfig(export_debounce=-1.0)
        assert "non-negative" in str(exc_info.value).lower()


# =============================================================================
# Baseline Tests (import from app.py)
# =============================================================================


# =============================================================================
# MemoryConfig: Expanded search_backend options (Memory V4)
# =============================================================================


class TestMemoryConfigQdrantExclusivity:
    """Test qdrant_path and qdrant_url mutual exclusivity."""

    def test_qdrant_path_only(self) -> None:
        """Test setting qdrant_path without qdrant_url."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(qdrant_path="/tmp/qdrant")
        assert config.qdrant_path == "/tmp/qdrant"
        assert config.qdrant_url is None

    def test_qdrant_url_only(self) -> None:
        """Test setting qdrant_url without qdrant_path."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(qdrant_url="http://localhost:6333")
        assert config.qdrant_url == "http://localhost:6333"
        assert config.qdrant_path is None

    def test_both_qdrant_rejected(self) -> None:
        """Test that setting both qdrant_path and qdrant_url raises error."""
        from gobby.config.persistence import MemoryConfig

        with pytest.raises(ValidationError, match="mutually exclusive"):
            MemoryConfig(qdrant_path="/tmp/qdrant", qdrant_url="http://localhost:6333")


class TestMemoryConfigEmbeddingFields:
    """Test embedding configuration fields."""

    def test_embedding_model_default(self) -> None:
        """Test default embedding_model value."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig()
        assert config.embedding_model == "text-embedding-3-small"

    def test_embedding_model_custom(self) -> None:
        """Test setting a custom embedding model."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(embedding_model="text-embedding-3-large")
        assert config.embedding_model == "text-embedding-3-large"


class TestMemoryConfigNeo4jFields:
    """Test neo4j_url and neo4j_auth fields on MemoryConfig."""

    def test_neo4j_url_defaults_to_none(self) -> None:
        """neo4j_url should default to None (opt-in via gobby install neo4j)."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig()
        assert config.neo4j_url is None

    def test_neo4j_auth_defaults_to_none(self) -> None:
        """neo4j_auth should default to None."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig()
        assert config.neo4j_auth is None

    def test_neo4j_url_accepts_valid_url(self) -> None:
        """Setting neo4j_url to a valid URL should work."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(neo4j_url="http://localhost:8474")
        assert config.neo4j_url == "http://localhost:8474"

    def test_neo4j_auth_stores_credentials(self) -> None:
        """neo4j_auth stores user:password format."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(neo4j_auth="neo4j:password")
        assert config.neo4j_auth == "neo4j:password"

    def test_neo4j_url_with_auth(self) -> None:
        """Both neo4j_url and neo4j_auth can be set together."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(
            neo4j_url="http://localhost:8474",
            neo4j_auth="neo4j:gobbyneo4j",
        )
        assert config.neo4j_url == "http://localhost:8474"
        assert config.neo4j_auth == "neo4j:gobbyneo4j"
