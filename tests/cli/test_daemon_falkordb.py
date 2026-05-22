"""Daemon service-start tests for FalkorDB wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.daemon import _services_start

pytestmark = pytest.mark.unit


def test_services_start_uses_falkordb_config_and_password(tmp_path: Path) -> None:
    services_dir = tmp_path / "services"
    services_dir.mkdir(parents=True)
    (services_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    bootstrap = MagicMock(falkordb_password="secret")
    config = MagicMock()
    config.databases.falkordb.host = "localhost"
    config.databases.falkordb.port = 6379
    config.databases.qdrant.url = None

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch("gobby.config.bootstrap.load_bootstrap", return_value=bootstrap),
        patch("gobby.config.app.load_config", return_value=config),
        patch("gobby.config.persistence.is_falkordb_enabled", return_value=True) as enabled,
        patch("gobby.cli.daemon.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        _services_start(tmp_path)

    enabled.assert_called_once_with(config.databases)
    cmd = mock_run.call_args.args[0]
    assert "--profile" in cmd
    assert "falkordb" in cmd
    assert "neo4j" not in cmd
    assert mock_run.call_args.kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "secret"
    assert "GOBBY_NEO4J_PASSWORD" not in mock_run.call_args.kwargs["env"]
