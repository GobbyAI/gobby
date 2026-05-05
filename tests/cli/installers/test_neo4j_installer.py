"""Tests for Neo4j installer."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# docker-compose.services.yml neo4j tests
# ---------------------------------------------------------------------------


class TestDockerComposeNeo4j:
    """Tests for the neo4j service in the unified docker-compose.services.yml."""

    def test_compose_file_exists(self) -> None:
        """docker-compose.services.yml exists in data directory."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        assert _COMPOSE_SRC.exists(), f"Expected {_COMPOSE_SRC} to exist"

    def test_compose_file_is_valid_yaml(self) -> None:
        """docker-compose.services.yml is valid YAML."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert isinstance(data, dict)

    def test_compose_has_neo4j_service(self) -> None:
        """docker-compose.services.yml defines a neo4j service."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "neo4j" in data["services"]

    def test_compose_neo4j_ports(self) -> None:
        """Neo4j service exposes ports 8474:7474 and 8687:7687."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        ports = data["services"]["neo4j"]["ports"]
        assert "8474:7474" in ports
        assert "8687:7687" in ports

    def test_compose_has_volume(self) -> None:
        """docker-compose.services.yml defines gobby_neo4j_data volume with explicit name."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "gobby_neo4j_data" in data.get("volumes", {})
        assert data["volumes"]["gobby_neo4j_data"]["name"] == "gobby_neo4j_data"

    def test_compose_has_logs_volume(self) -> None:
        """docker-compose.services.yml defines gobby_neo4j_logs volume to prevent anonymous volumes."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "gobby_neo4j_logs" in data.get("volumes", {})
        assert data["volumes"]["gobby_neo4j_logs"]["name"] == "gobby_neo4j_logs"

    def test_compose_has_healthcheck(self) -> None:
        """Neo4j service has a healthcheck."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "healthcheck" in data["services"]["neo4j"]

    def test_compose_has_apoc_plugin(self) -> None:
        """Neo4j service enables APOC plugin."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        env = data["services"]["neo4j"]["environment"]
        env_str = str(env)
        assert "apoc" in env_str.lower()

    def test_compose_has_conf_volume_mount(self) -> None:
        """Neo4j service bind-mounts ./neo4j/conf for APOC config persistence."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        volumes = data["services"]["neo4j"]["volumes"]
        assert "./neo4j/conf:/var/lib/neo4j/conf" in volumes

    def test_compose_neo4j_has_profiles(self) -> None:
        """Neo4j service has 'neo4j' and 'all' profiles."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        profiles = data["services"]["neo4j"]["profiles"]
        assert "neo4j" in profiles
        assert "all" in profiles

    def test_compose_neo4j_uses_latest_tag(self) -> None:
        """Neo4j service uses neo4j:latest."""
        from gobby.cli.installers.neo4j import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        image = data["services"]["neo4j"]["image"]
        assert image == "neo4j:latest"


# ---------------------------------------------------------------------------
# Installer function tests
# ---------------------------------------------------------------------------


class TestInstallNeo4j:
    """Tests for install_neo4j function."""

    def test_install_neo4j_no_docker(self, tmp_path: Path) -> None:
        """install_neo4j returns error when Docker is not available."""
        from gobby.cli.installers.neo4j import install_neo4j

        with patch.object(shutil, "which", return_value=None):
            result = install_neo4j(gobby_home=tmp_path)

        assert result["success"] is False
        assert "Docker" in result["error"]

    def test_install_neo4j_copies_compose_file(self, tmp_path: Path) -> None:
        """install_neo4j copies docker-compose.yml to services/."""
        from gobby.cli.installers.neo4j import install_neo4j

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.neo4j.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.neo4j._wait_for_health", return_value=True),
            patch("gobby.cli.installers.neo4j._update_config"),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            result = install_neo4j(gobby_home=tmp_path)

        assert result["success"] is True
        compose_dest = tmp_path / "services" / "docker-compose.yml"
        assert compose_dest.exists()

    def test_install_neo4j_creates_conf_directory(self, tmp_path: Path) -> None:
        """install_neo4j creates neo4j/conf/neo4j.conf with APOC config."""
        from gobby.cli.installers.neo4j import install_neo4j

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.neo4j.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.neo4j._wait_for_health", return_value=True),
            patch("gobby.cli.installers.neo4j._update_config"),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            install_neo4j(gobby_home=tmp_path)

        conf_file = tmp_path / "services" / "neo4j" / "conf" / "neo4j.conf"
        assert conf_file.exists()
        content = conf_file.read_text()
        assert "dbms.security.procedures.unrestricted=apoc.*" in content

    def test_install_neo4j_does_not_overwrite_existing_conf(self, tmp_path: Path) -> None:
        """install_neo4j preserves existing neo4j/conf/neo4j.conf."""
        from gobby.cli.installers.neo4j import install_neo4j

        conf_dir = tmp_path / "services" / "neo4j" / "conf"
        conf_dir.mkdir(parents=True)
        conf_file = conf_dir / "neo4j.conf"
        conf_file.write_text("# user customized config\n")

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.neo4j.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.neo4j._wait_for_health", return_value=True),
            patch("gobby.cli.installers.neo4j._update_config"),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            install_neo4j(gobby_home=tmp_path)

        assert conf_file.read_text() == "# user customized config\n"

    def test_install_neo4j_calls_docker_compose_with_profile(self, tmp_path: Path) -> None:
        """install_neo4j runs docker compose up with --profile neo4j."""
        from gobby.cli.installers.neo4j import install_neo4j

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.neo4j.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.neo4j._wait_for_health", return_value=True),
            patch("gobby.cli.installers.neo4j._update_config"),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            install_neo4j(gobby_home=tmp_path)

        call_args = mock_subprocess.run.call_args
        cmd = call_args[0][0]
        assert "docker" in cmd
        assert "--profile" in cmd
        assert "neo4j" in cmd
        assert "up" in cmd
        assert "-d" in cmd

    def test_install_neo4j_updates_config(self, tmp_path: Path) -> None:
        """install_neo4j updates daemon config with neo4j_url."""
        from gobby.cli.installers.neo4j import install_neo4j

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.neo4j.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.neo4j._wait_for_health", return_value=True),
            patch("gobby.cli.installers.neo4j._update_config") as mock_update,
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            install_neo4j(gobby_home=tmp_path)

        mock_update.assert_called_once()
        kwargs = mock_update.call_args[1]
        assert "neo4j_url" in kwargs
        assert kwargs["neo4j_url"] is not None

    def test_install_neo4j_returns_error_on_compose_failure(self, tmp_path: Path) -> None:
        """install_neo4j returns error when docker compose up fails."""
        from gobby.cli.installers.neo4j import install_neo4j

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.neo4j.subprocess") as mock_subprocess,
        ):
            mock_subprocess.run.return_value = MagicMock(
                returncode=1, stderr="container failed", stdout=""
            )
            mock_subprocess.TimeoutExpired = TimeoutError

            result = install_neo4j(gobby_home=tmp_path)

        assert result["success"] is False


class TestUninstallNeo4j:
    """Tests for uninstall_neo4j function."""

    def test_uninstall_neo4j_not_installed(self, tmp_path: Path) -> None:
        """uninstall_neo4j returns success when not installed."""
        from gobby.cli.installers.neo4j import uninstall_neo4j

        with patch("gobby.cli.installers.neo4j._update_config"):
            result = uninstall_neo4j(gobby_home=tmp_path)

        assert result["success"] is True
        assert result.get("already_uninstalled") is True

    def test_uninstall_neo4j_runs_compose_down_with_profile(self, tmp_path: Path) -> None:
        """uninstall_neo4j runs docker compose down with --profile neo4j."""
        from gobby.cli.installers.neo4j import uninstall_neo4j

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        compose_file = svc_dir / "docker-compose.yml"
        compose_file.write_text("services: {}")

        with (
            patch("gobby.cli.installers.neo4j.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.neo4j._update_config"),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            result = uninstall_neo4j(gobby_home=tmp_path)

        assert result["success"] is True
        call_args = mock_subprocess.run.call_args
        cmd = call_args[0][0]
        assert "--profile" in cmd
        assert "neo4j" in cmd
        assert "down" in cmd

    def test_uninstall_neo4j_with_volumes(self, tmp_path: Path) -> None:
        """uninstall_neo4j removes named volumes when remove_volumes=True."""
        from gobby.cli.installers.neo4j import uninstall_neo4j

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        compose_file = svc_dir / "docker-compose.yml"
        compose_file.write_text("services: {}")

        with (
            patch("gobby.cli.installers.neo4j.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.neo4j._update_config"),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            uninstall_neo4j(gobby_home=tmp_path, remove_volumes=True)

        # Should have called docker compose down + 2 volume rm calls
        assert mock_subprocess.run.call_count == 3
        volume_cmds = [call[0][0] for call in mock_subprocess.run.call_args_list[1:]]
        volume_names = [cmd[-1] for cmd in volume_cmds]
        assert "gobby_neo4j_data" in volume_names
        assert "gobby_neo4j_logs" in volume_names

    def test_uninstall_neo4j_resets_config(self, tmp_path: Path) -> None:
        """uninstall_neo4j resets neo4j config to None."""
        from gobby.cli.installers.neo4j import uninstall_neo4j

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        compose_file = svc_dir / "docker-compose.yml"
        compose_file.write_text("services: {}")

        with (
            patch("gobby.cli.installers.neo4j.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.neo4j._update_config") as mock_update,
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            uninstall_neo4j(gobby_home=tmp_path)

        mock_update.assert_called_once_with(neo4j_url=None, neo4j_auth=None)
        assert mock_update.call_count == 1
        assert mock_update.call_args is not None

    def test_uninstall_neo4j_cleans_conf_dir(self, tmp_path: Path) -> None:
        """uninstall_neo4j removes neo4j conf subdirectory but leaves services/ intact."""
        from gobby.cli.installers.neo4j import uninstall_neo4j

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        compose_file = svc_dir / "docker-compose.yml"
        compose_file.write_text("services: {}")
        neo4j_conf = svc_dir / "neo4j" / "conf"
        neo4j_conf.mkdir(parents=True)
        (neo4j_conf / "neo4j.conf").write_text("# config")

        with (
            patch("gobby.cli.installers.neo4j.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.neo4j._update_config"),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            uninstall_neo4j(gobby_home=tmp_path)

        assert not (svc_dir / "neo4j").exists()
        assert svc_dir.exists()
        assert compose_file.exists()
