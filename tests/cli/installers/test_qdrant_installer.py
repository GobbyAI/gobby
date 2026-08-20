"""Tests for Qdrant installer and unified Docker Compose template."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# docker-compose.services.yml tests
# ---------------------------------------------------------------------------


class TestDockerComposeServices:
    """Tests for the unified docker-compose.services.yml file."""

    def test_compose_file_exists(self) -> None:
        """docker-compose.services.yml exists in data directory."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        assert _COMPOSE_SRC.exists(), f"Expected {_COMPOSE_SRC} to exist"

    def test_compose_file_is_valid_yaml(self) -> None:
        """docker-compose.services.yml is valid YAML."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert isinstance(data, dict)

    def test_compose_has_qdrant_service(self) -> None:
        """Compose file defines a qdrant service."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "qdrant" in data["services"]

    def test_compose_has_falkordb_service(self) -> None:
        """Compose file defines a falkordb service."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "falkordb" in data["services"]
        assert "neo4j" not in data["services"]

    def test_falkordb_service_contract(self) -> None:
        """FalkorDB service uses the expected image, ports, auth, and healthcheck."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        falkordb = data["services"]["falkordb"]

        assert falkordb["image"] == "falkordb/falkordb:latest"
        assert (
            "${GOBBY_SERVICES_BIND_ADDRESS:-127.0.0.1}:${GOBBY_FALKORDB_PORT:-16379}:6379"
        ) in falkordb["ports"]
        assert "127.0.0.1:${GOBBY_FALKORDB_BROWSER_PORT:-13000}:3000" in falkordb["ports"]
        assert (
            "REDIS_ARGS=--requirepass ${GOBBY_FALKORDB_PASSWORD:-gobbyfalkor} --save 3600 1 300 100"
            in falkordb["environment"]
        )
        assert (
            "FALKORDB_ARGS=MAX_QUEUED_QUERIES 25 TIMEOUT_DEFAULT 30000 TIMEOUT_MAX 0 RESULTSET_SIZE 10000"
            in falkordb["environment"]
        )
        assert (
            "GOBBY_FALKORDB_PASSWORD=${GOBBY_FALKORDB_PASSWORD:-gobbyfalkor}"
            in falkordb["environment"]
        )
        assert falkordb["volumes"] == ["gobby_falkordb_data:/var/lib/falkordb/data"]
        assert falkordb["healthcheck"]["test"] == [
            "CMD-SHELL",
            'redis-cli -a "$$GOBBY_FALKORDB_PASSWORD" PING | grep -q PONG',
        ]

    def test_qdrant_ports(self) -> None:
        """Qdrant service exposes HTTP and gRPC ports."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        ports = data["services"]["qdrant"]["ports"]
        assert ports == [
            "${GOBBY_SERVICES_BIND_ADDRESS:-127.0.0.1}:${GOBBY_QDRANT_HTTP_PORT:-6333}:6333",
            "${GOBBY_SERVICES_BIND_ADDRESS:-127.0.0.1}:${GOBBY_QDRANT_GRPC_PORT:-6334}:6334",
        ]

    def test_qdrant_has_healthcheck(self) -> None:
        """Qdrant service has a healthcheck."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "healthcheck" in data["services"]["qdrant"]

    def test_qdrant_healthcheck_uses_healthz(self) -> None:
        """Qdrant healthcheck uses /healthz endpoint."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        test_cmd = data["services"]["qdrant"]["healthcheck"]["test"]
        assert any("healthz" in str(t) for t in test_cmd)

    def test_qdrant_uses_named_volume(self) -> None:
        """Qdrant uses named Docker volume for storage."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        volumes = data["services"]["qdrant"]["volumes"]
        assert any("gobby_qdrant_data:/qdrant/storage" in str(v) for v in volumes)

    def test_qdrant_has_profiles(self) -> None:
        """Qdrant service has docker compose profiles."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        profiles = data["services"]["qdrant"]["profiles"]
        assert "qdrant" in profiles
        assert "all" in profiles

    def test_falkordb_has_profiles(self) -> None:
        """FalkorDB service has docker compose profiles."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        profiles = data["services"]["falkordb"]["profiles"]
        assert "falkordb" in profiles
        assert "all" in profiles

    def test_compose_has_falkordb_volume(self) -> None:
        """Compose file defines gobby_falkordb_data volume with explicit name."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "gobby_falkordb_data" in data.get("volumes", {})
        assert data["volumes"]["gobby_falkordb_data"]["name"] == "gobby_falkordb_data"
        assert "gobby_neo4j_data" not in data.get("volumes", {})
        assert "gobby_neo4j_logs" not in data.get("volumes", {})

    def test_compose_has_qdrant_volume(self) -> None:
        """Compose file defines gobby_qdrant_data volume with explicit name."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "gobby_qdrant_data" in data.get("volumes", {})
        assert data["volumes"]["gobby_qdrant_data"]["name"] == "gobby_qdrant_data"

    def test_qdrant_restart_policy(self) -> None:
        """Qdrant service has unless-stopped restart policy."""
        from gobby.cli.installers.qdrant import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert data["services"]["qdrant"]["restart"] == "unless-stopped"


# ---------------------------------------------------------------------------
# Installer function tests
# ---------------------------------------------------------------------------


class TestInstallQdrant:
    """Tests for install_qdrant function."""

    def test_install_qdrant_no_docker(self, tmp_path: Path) -> None:
        """install_qdrant returns error when Docker is not available."""
        from gobby.cli.installers.qdrant import install_qdrant

        with patch.object(shutil, "which", return_value=None):
            result = install_qdrant(gobby_home=tmp_path)

        assert result["success"] is False
        assert "Docker not found" in result["error"]

    def test_install_creates_compose_file(self, tmp_path: Path) -> None:
        """install_qdrant copies compose template to services directory."""
        from gobby.cli.installers.postgres import reconcile_unified_compose

        services_dir = tmp_path / "services"
        services_dir.mkdir()
        compose_file = reconcile_unified_compose(services_dir).compose_file

        assert compose_file.exists()
        assert compose_file.name == "docker-compose.yml"
        data = yaml.safe_load(compose_file.read_text())
        assert "qdrant" in data["services"]

    def test_install_exports_and_checks_custom_port(self, tmp_path: Path) -> None:
        """install_qdrant uses the persisted custom port for compose and health checks."""
        from gobby.cli.installers.qdrant import install_qdrant

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.qdrant.subprocess.run", return_value=mock_result) as run,
            patch(
                "gobby.cli.installers.qdrant._wait_for_health", return_value=True
            ) as wait_for_health,
            patch("gobby.cli.installers.qdrant._update_config") as update_config,
            patch("gobby.cli.installers.qdrant.resolve_compose_runtime") as resolve,
        ):
            resolve.return_value.environment = {
                "GOBBY_QDRANT_HTTP_PORT": "7333",
                "GOBBY_POSTGRES_PASSWORD": "postgres-secret",
            }
            result = install_qdrant(gobby_home=tmp_path, port=7333)

        assert result["qdrant_url"] == "http://localhost:7333"
        assert result["success"] is True
        update_config.assert_called_once_with(
            qdrant_port=7333,
            gobby_home=tmp_path,
        )
        resolve.assert_called_once_with(tmp_path, profiles=("qdrant",))
        assert run.call_args.kwargs["env"]["GOBBY_QDRANT_HTTP_PORT"] == "7333"
        wait_for_health.assert_called_once_with("http://localhost:7333")

    def test_install_health_check_failure(self, tmp_path: Path) -> None:
        """install_qdrant returns error when health check fails."""
        from gobby.cli.installers.qdrant import install_qdrant

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.qdrant.subprocess.run", return_value=mock_result),
            patch("gobby.cli.installers.qdrant._wait_for_health", return_value=False),
            patch("gobby.cli.installers.qdrant._update_config"),
            patch("gobby.cli.installers.qdrant.resolve_compose_runtime") as resolve,
        ):
            resolve.return_value.environment = {"GOBBY_QDRANT_HTTP_PORT": "6333"}
            result = install_qdrant(gobby_home=tmp_path)

        assert result["success"] is False
        assert "Health check failed" in result["error"]


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------


class TestQdrantHealthCheck:
    """Tests for Qdrant health check."""

    @pytest.mark.asyncio
    async def test_is_qdrant_healthy_none_url(self) -> None:
        """Returns False for None URL."""
        from gobby.cli.services import is_qdrant_healthy

        assert await is_qdrant_healthy(None) is False

    @pytest.mark.asyncio
    async def test_is_qdrant_installed_no_files(self, tmp_path: Path) -> None:
        """Returns False when no compose file exists."""
        from gobby.cli.services import is_qdrant_installed

        assert is_qdrant_installed(gobby_home=tmp_path) is False

    def test_is_qdrant_installed_with_files(self, tmp_path: Path) -> None:
        """Returns True when compose file exists."""
        from gobby.cli.services import is_qdrant_installed

        services = tmp_path / "services"
        services.mkdir(parents=True)
        (services / "docker-compose.yml").write_text("services: {}")

        assert is_qdrant_installed(gobby_home=tmp_path) is True

    def test_is_qdrant_installed_uses_gobby_home_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Uses get_gobby_home when no explicit home is supplied."""
        import gobby.cli.services as services_module

        services = tmp_path / "services"
        services.mkdir(parents=True)
        (services / "docker-compose.yml").write_text("services: {}")
        monkeypatch.setattr(services_module, "get_gobby_home", lambda: tmp_path)

        assert services_module.is_qdrant_installed() is True


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


class TestConfigModels:
    """Tests for new DatabasesConfig and EmbeddingsConfig models."""

    def test_databases_config_defaults(self) -> None:
        """DatabasesConfig has sensible defaults."""
        from gobby.config.persistence import DatabasesConfig

        config = DatabasesConfig()
        assert config.qdrant.url is None
        assert config.qdrant.port == 6333
        assert config.falkordb.host == "127.0.0.1"
        assert config.falkordb.port == 16379
        assert config.falkordb.password is None
        assert config.falkordb.graph_name == "gobby_kg"

    def test_embeddings_config_defaults(self) -> None:
        """EmbeddingsConfig has sensible defaults."""
        from gobby.config.persistence import EmbeddingsConfig

        config = EmbeddingsConfig()
        assert config.model == "nomic-embed-text"
        assert config.dim == 768

    def test_qdrant_config_requires_explicit_url(self) -> None:
        """QdrantConfig leaves its required managed URL unset until install."""
        from gobby.config.persistence import QdrantConfig

        config = QdrantConfig()
        assert config.url is None

    def test_daemon_config_has_databases(self) -> None:
        """DaemonConfig includes databases and embeddings."""
        from gobby.config.app import DaemonConfig

        config = DaemonConfig()
        assert hasattr(config, "databases")
        assert hasattr(config, "embeddings")
        assert config.databases.qdrant.port == 6333
        assert config.embeddings.dim == 768

    def test_memory_config_has_no_database_fields(self) -> None:
        """MemoryConfig no longer contains database or embedding fields."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig()
        assert not hasattr(config, "qdrant_url")
        assert not hasattr(config, "neo4j_url")
        assert not hasattr(config, "embedding_model")
