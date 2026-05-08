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
        assert config.access_debounce_seconds == 60
        assert config.crossref_threshold == 0.3
        assert config.kg.provider == "claude"
        assert config.kg.model == "haiku"
        assert config.stale_audit.prompt_path == "memory/stale_audit"
        assert config.stale_audit.model == "haiku"


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

    def test_custom_access_debounce(self) -> None:
        """Test setting custom access_debounce_seconds."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(access_debounce_seconds=120)
        assert config.access_debounce_seconds == 120


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


class TestQdrantConfigDefaults:
    """Test QdrantConfig default values."""

    def test_qdrant_url_defaults_to_localhost(self) -> None:
        """Test that QdrantConfig.url defaults to http://localhost:6333."""
        from gobby.config.persistence import QdrantConfig

        config = QdrantConfig()
        assert config.url == "http://localhost:6333"

    def test_qdrant_api_key_defaults_to_none(self) -> None:
        """Test that QdrantConfig.api_key defaults to None."""
        from gobby.config.persistence import QdrantConfig

        config = QdrantConfig()
        assert config.api_key is None


class TestEmbeddingsConfigFields:
    """Test EmbeddingsConfig fields (moved from MemoryConfig)."""

    def test_embedding_model_default(self) -> None:
        """Test default model value."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig()
        assert config.model == "nomic-embed-text"

    def test_embedding_model_custom(self) -> None:
        """Test setting a custom embedding model."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig(model="text-embedding-3-large")
        assert config.model == "text-embedding-3-large"

    def test_embedding_api_base_default(self) -> None:
        """Test default api_base is None."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig()
        assert config.api_base is None

    def test_embedding_api_base_custom(self) -> None:
        """Test setting custom api_base for local models."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig(api_base="http://localhost:11434/v1")
        assert config.api_base == "http://localhost:11434/v1"

    def test_embedding_api_key_default(self) -> None:
        """Test default api_key is None."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig()
        assert config.api_key is None

    def test_embedding_api_key_custom(self) -> None:
        """Test setting custom api_key."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig(api_key="sk-custom-key")
        assert config.api_key == "sk-custom-key"

    def test_embedding_dim_default(self) -> None:
        """Test default dim is 768 (nomic-embed-text-v1.5)."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig()
        assert config.dim == 768

    def test_embedding_dim_custom(self) -> None:
        """Test setting custom dim for cloud models."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig(dim=1536)
        assert config.dim == 1536

    def test_embedding_dim_must_be_positive(self) -> None:
        """Test that dim must be at least 1."""
        from gobby.config.persistence import EmbeddingsConfig

        with pytest.raises(ValidationError):
            EmbeddingsConfig(dim=0)

        with pytest.raises(ValidationError):
            EmbeddingsConfig(dim=-1)

    def test_local_embedding_config_full(self) -> None:
        """Test full local embedding configuration (e.g., Ollama + nomic-embed-text)."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig(
            model="openai/nomic-embed-text",
            api_base="http://localhost:11434/v1",
            dim=768,
        )
        assert config.model == "openai/nomic-embed-text"
        assert config.api_base == "http://localhost:11434/v1"
        assert config.dim == 768


class TestNeo4jConfigFields:
    """Test Neo4jConfig fields (moved from MemoryConfig)."""

    def test_neo4j_url_defaults_to_docker_compose(self) -> None:
        """url should default to the gobby docker-compose port mapping."""
        from gobby.config.persistence import Neo4jConfig

        config = Neo4jConfig()
        assert config.url == "http://localhost:8474"

    def test_neo4j_auth_defaults_to_docker_compose(self) -> None:
        """auth should default to None (must be provided when enabled)."""
        from gobby.config.persistence import Neo4jConfig

        config = Neo4jConfig()
        assert config.auth is None

    def test_neo4j_url_accepts_valid_url(self) -> None:
        """Setting url to a valid URL should work."""
        from gobby.config.persistence import Neo4jConfig

        config = Neo4jConfig(url="http://localhost:8474")
        assert config.url == "http://localhost:8474"

    def test_neo4j_auth_stores_credentials(self) -> None:
        """auth stores user:password format."""
        from gobby.config.persistence import Neo4jConfig

        config = Neo4jConfig(auth="neo4j:password")
        assert config.auth == "neo4j:password"

    def test_neo4j_url_with_auth(self) -> None:
        """Both url and auth can be set together."""
        from gobby.config.persistence import Neo4jConfig

        config = Neo4jConfig(
            url="http://localhost:8474",
            auth="neo4j:gobbyneo4j",
        )
        assert config.url == "http://localhost:8474"
        assert config.auth == "neo4j:gobbyneo4j"
