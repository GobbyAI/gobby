"""Tests for config/persistence.py module."""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from gobby.config.feature_base import (
    FeatureProfile,
    candidate_labels,
    default_candidates_for_profile,
)

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
        assert config.kg.profile == FeatureProfile.LOW
        assert candidate_labels(config.kg.candidates) == default_candidates_for_profile(
            FeatureProfile.LOW
        )
        assert config.dream.prompt_path == "memory/dream"
        assert config.dream.schedule_cron == "0 2 * * *"
        assert config.dream.min_action_confidence == 0.72
        assert config.shadow_relevance_judging is False
        assert "digest_memory_usefulness" not in MemoryConfig.model_fields


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

    def test_shadow_relevance_judging_requires_signal_hub(self) -> None:
        """Shadow judging cannot start without its durable request source."""
        from gobby.config.persistence import MemoryConfig

        with pytest.raises(ValidationError, match="recall_signal_hub"):
            MemoryConfig(shadow_relevance_judging=True)

        config = MemoryConfig(
            shadow_relevance_judging=True,
            recall_signal_hub=True,
        )
        assert config.shadow_relevance_judging is True


class TestMemoryKnowledgeGraphConfig:
    """Test memory knowledge graph config fields."""

    def test_max_rebuild_concurrency_default(self) -> None:
        from gobby.config.persistence import MemoryKnowledgeGraphConfig

        config = MemoryKnowledgeGraphConfig()

        assert config.max_rebuild_concurrency == 2

    def test_max_rebuild_concurrency_override(self) -> None:
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig(kg={"max_rebuild_concurrency": 1})

        assert config.kg.max_rebuild_concurrency == 1

    def test_max_rebuild_concurrency_validation(self) -> None:
        from gobby.config.persistence import MemoryKnowledgeGraphConfig

        with pytest.raises(ValidationError):
            MemoryKnowledgeGraphConfig(max_rebuild_concurrency=0)
        with pytest.raises(ValidationError):
            MemoryKnowledgeGraphConfig(max_rebuild_concurrency=-1)


# =============================================================================
# MemoryDreamConfig Tests (dream GC: page/cooldown/purge/retention)
# =============================================================================


class TestMemoryDreamConfig:
    """Test MemoryDreamConfig GC fields added for the self-healing sweep."""

    def test_dream_gc_field_defaults(self) -> None:
        """New GC knobs carry the design-plan defaults."""
        from gobby.config.persistence import MemoryDreamConfig

        config = MemoryDreamConfig()
        assert config.planner_batch_max_chars == 100_000
        assert config.redream_after_hours == 20
        assert config.purge_delete_after_days == 30
        assert config.purge_review_after_days == 90
        assert config.run_retention_days == 30
        assert config.min_rescope_confidence == 0.85

    def test_dream_batch_window_field_defaults(self) -> None:
        """The work-unit scheduler knobs carry the redesign defaults."""
        from gobby.config.persistence import MemoryDreamConfig

        config = MemoryDreamConfig()
        assert config.schedule_cron == "0 2 * * *"
        assert config.planner_batch_size == 25
        assert config.max_runtime_seconds == 14400
        assert config.work_unit_timeout_seconds == 1500.0
        assert config.evidence_channel_timeout_seconds == 30.0
        assert config.evidence_retry_attempts == 3
        assert config.evidence_phase_timeout_seconds == 210.0

    def test_related_evidence_field_defaults(self) -> None:
        """Dream evidence and write-trigger knobs use the reconciliation defaults."""
        from gobby.config.persistence import MemoryDreamConfig

        config = MemoryDreamConfig()
        assert config.related_evidence_enabled is True
        assert config.related_evidence_top_k == 3
        assert config.related_evidence_fetch_limit == 10
        assert config.planner_batch_max_chars == 100_000
        assert config.write_supersession_mark_due_enabled is True

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("related_evidence_top_k", 0),
            ("related_evidence_top_k", 11),
            ("related_evidence_fetch_limit", 0),
            ("related_evidence_fetch_limit", 51),
            ("planner_batch_max_chars", 9_999),
        ],
    )
    def test_related_evidence_fields_enforce_bounds(self, field: str, value: int) -> None:
        """Bounded retrieval and planner settings reject unsafe values."""
        from gobby.config.persistence import MemoryDreamConfig

        with pytest.raises(ValidationError):
            MemoryDreamConfig(**{field: value})

    @pytest.mark.parametrize(
        "field",
        [
            "planner_batch_size",
            "planner_batch_max_chars",
            "max_runtime_seconds",
            "evidence_retry_attempts",
            "redream_after_hours",
            "purge_delete_after_days",
            "purge_review_after_days",
            "run_retention_days",
        ],
    )
    def test_dream_int_fields_reject_non_positive(self, field: str) -> None:
        """Each batch/window int is registered in validate_positive_int."""
        from gobby.config.persistence import MemoryDreamConfig

        with pytest.raises(ValidationError):
            MemoryDreamConfig(**{field: 0})

    @pytest.mark.parametrize(
        "field",
        [
            "work_unit_timeout_seconds",
            "evidence_channel_timeout_seconds",
            "evidence_phase_timeout_seconds",
        ],
    )
    def test_dream_timeout_fields_reject_non_positive(self, field: str) -> None:
        """Each timeout float is registered in validate_positive_float."""
        from gobby.config.persistence import MemoryDreamConfig

        with pytest.raises(ValidationError):
            MemoryDreamConfig(**{field: 0.0})

    @pytest.mark.parametrize(
        "field",
        [
            "allow_unattended_mutations",
            "planner_max_concurrency",
            "page_size",
            "candidate_page_timeout_seconds",
            "scan_limit",
            "max_scan_rows",
            "stale_age_days",
        ],
    )
    def test_removed_page_era_fields_rejected(self, field: str) -> None:
        """Pre-0.5 schema change: removed page-era and deprecated fields no
        longer load — the model forbids extras."""
        from gobby.config.persistence import MemoryDreamConfig

        with pytest.raises(ValidationError):
            MemoryDreamConfig(**{field: 1})

    def test_unknown_field_still_forbidden(self) -> None:
        """The base model forbids extras."""
        from gobby.config.persistence import MemoryDreamConfig

        with pytest.raises(ValidationError):
            MemoryDreamConfig(not_a_real_dream_field=1)


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
        assert config.backup_path == Path(".gobby/memories.jsonl")


class TestMemoryBackupConfigCustom:
    """Test MemoryBackupConfig with custom values."""

    def test_disabled_backup(self) -> None:
        """Test disabling memory backup tools."""
        from gobby.config.persistence import MemoryBackupConfig

        config = MemoryBackupConfig(enabled=False)
        assert config.enabled is False

    def test_custom_backup_path(self) -> None:
        """Test setting a custom backup path."""
        from pathlib import Path

        from gobby.config.persistence import MemoryBackupConfig

        config = MemoryBackupConfig(backup_path=Path("/custom/memories.jsonl"))
        assert config.backup_path == Path("/custom/memories.jsonl")


# =============================================================================
# Baseline Tests (import from app.py)
# =============================================================================


# =============================================================================
# MemoryConfig: Expanded search_backend options (Memory V4)
# =============================================================================


class TestQdrantConfigDefaults:
    """Test QdrantConfig default values."""

    def test_qdrant_url_requires_managed_install_config(self) -> None:
        """QdrantConfig.url remains unset until the managed installer persists it."""
        from gobby.config.persistence import QdrantConfig

        config = QdrantConfig()
        assert config.url is None

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

    def test_falkor_password_defaults_to_none(self) -> None:
        """password defaults to None so unconfigured installs stay disabled."""
        from gobby.config.persistence import FalkorConfig

        config = FalkorConfig()
        assert config.password is None

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
        assert FalkorConfig(password=password).password == password

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
            FalkorConfig(password=password)

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

    def test_is_falkordb_enabled_requires_password(self) -> None:
        """Only a resolved password value enables the graph backend."""
        from gobby.config.persistence import DatabasesConfig, is_falkordb_enabled

        assert is_falkordb_enabled(DatabasesConfig()) is False
        assert is_falkordb_enabled(DatabasesConfig(falkordb={"password": "secret"})) is True

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
