"""Tests for config/persistence.py module."""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

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

    def test_backend_rejects_postgres(self) -> None:
        """PostgreSQL is configured as the hub, not as a memory backend."""
        from gobby.config.persistence import MemoryConfig

        with pytest.raises(ValidationError) as exc_info:
            MemoryConfig(backend="postgres")
        assert "postgres" in str(exc_info.value).lower()


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

    def test_provider_field_removed(self) -> None:
        """provider is no longer part of the runtime embedding config surface."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig()

        assert "provider" not in EmbeddingsConfig.model_fields
        assert not hasattr(config, "provider")


class TestFalkorConfigFields:
    """Test FalkorConfig fields."""

    def test_falkor_host_defaults_to_loopback(self) -> None:
        """host should default to the Docker port-mapped loopback address."""
        from gobby.config.persistence import FalkorConfig

        config = FalkorConfig()
        assert config.host == "127.0.0.1"

    def test_falkor_port_defaults_to_remapped_redis_port(self) -> None:
        """port should default to the host-side FalkorDB Redis port."""
        from gobby.config.persistence import FalkorConfig

        config = FalkorConfig()
        assert config.port == 16379

    def test_falkor_requirepass_defaults_to_none(self) -> None:
        """requirepass defaults to None so unconfigured installs stay disabled."""
        from gobby.config.persistence import FalkorConfig

        config = FalkorConfig()
        assert config.requirepass is None

    @pytest.mark.parametrize(
        "password",
        [
            "Pa$$w0rd!",
            "aB-3.7=z",
            "xyz_123-ABC",
        ],
    )
    def test_validate_falkordb_password_accepts_printable_ascii(self, password: str) -> None:
        """Accepted FalkorDB passwords are printable ASCII without whitespace."""
        from gobby.config.persistence import FalkorConfig, validate_falkordb_password

        assert validate_falkordb_password(password) == password
        assert FalkorConfig(requirepass=password).requirepass == password

    @pytest.mark.parametrize(
        ("password", "message"),
        [
            ("", "must not be empty"),
            ("has space", "must not contain whitespace"),
            ("has\ttab", "must not contain whitespace"),
            ("has\nnewline", "must not contain whitespace"),
            ("has\x00control", "must not contain ASCII control characters"),
            ("has-é-high-bit", "must use printable ASCII only"),
        ],
    )
    def test_validate_falkordb_password_rejects_docker_unsafe_values(
        self, password: str, message: str
    ) -> None:
        """Docker-unsafe FalkorDB passwords are rejected with actionable messages."""
        from gobby.config.persistence import FalkorConfig, validate_falkordb_password

        with pytest.raises(ValueError, match=message):
            validate_falkordb_password(password)
        with pytest.raises(ValidationError, match=message):
            FalkorConfig(requirepass=password)

    def test_falkor_graph_name_defaults_to_memory_graph(self) -> None:
        """graph_name defaults to the memory knowledge graph."""
        from gobby.config.persistence import FalkorConfig

        config = FalkorConfig()
        assert config.graph_name == "gobby_kg"

    def test_falkor_graph_min_score_validation(self) -> None:
        """graph_min_score keeps the previous 0.0-1.0 validation contract."""
        from gobby.config.persistence import FalkorConfig

        assert FalkorConfig(graph_min_score=0.0).graph_min_score == 0.0
        assert FalkorConfig(graph_min_score=1.0).graph_min_score == 1.0
        with pytest.raises(ValidationError):
            FalkorConfig(graph_min_score=-0.1)
        with pytest.raises(ValidationError):
            FalkorConfig(graph_min_score=1.1)

    def test_databases_config_uses_falkordb_not_neo4j(self) -> None:
        """DatabasesConfig exposes falkordb and drops the legacy neo4j field."""
        from gobby.config.persistence import DatabasesConfig, FalkorConfig

        config = DatabasesConfig()
        assert isinstance(config.falkordb, FalkorConfig)
        assert not hasattr(config, "neo4j")

    def test_is_falkordb_enabled_requires_requirepass(self) -> None:
        """Only a resolved requirepass value enables the graph backend."""
        from gobby.config.persistence import DatabasesConfig, is_falkordb_enabled

        assert is_falkordb_enabled(DatabasesConfig()) is False
        assert is_falkordb_enabled(DatabasesConfig(falkordb={"requirepass": "secret"})) is True

    def test_neo4j_config_symbol_is_removed(self) -> None:
        """FalkorConfig replaces the exported Neo4jConfig symbol."""
        import gobby.config.persistence as persistence

        assert not hasattr(persistence, "Neo4jConfig")


class TestFalkorDependencyLock:
    """Tests for the pyproject.toml and uv.lock dependency contract."""

    def test_pyproject_declares_falkordb_dependency_and_drops_neo4j(self) -> None:
        """The runtime dependency set should name falkordb and omit neo4j."""
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        dependencies = data["project"]["dependencies"]

        assert any(dep.startswith("falkordb>=1.1.0") for dep in dependencies)
        assert not any(dep.startswith("neo4j") for dep in dependencies)

    def test_uv_lock_contains_falkordb_and_gobby_dep_entry(self) -> None:
        """The lockfile should be regenerated with falkordb in the resolved graph."""
        data = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
        packages = data["package"]
        package_names = {package["name"] for package in packages}
        gobby_package = next(package for package in packages if package["name"] == "gobby")
        gobby_deps = {dependency["name"] for dependency in gobby_package["dependencies"]}

        assert "falkordb" in package_names
        assert "falkordb" in gobby_deps
        assert "neo4j" not in package_names
        assert "neo4j" not in gobby_deps
