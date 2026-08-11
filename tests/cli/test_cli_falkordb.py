"""Tests for FalkorDB CLI flags and legacy Neo4j migration errors."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _option_names(command: click.Command) -> set[str]:
    return {
        opt for param in command.params if isinstance(param, click.Option) for opt in param.opts
    }


def _param_by_name(command: click.Command, name: str) -> click.Option:
    for param in command.params:
        if isinstance(param, click.Option) and param.name == name:
            return param
    raise AssertionError(f"missing Click option parameter {name!r}")


class TestFalkorDBInstallFlags:
    def test_install_exposes_only_falkordb_password_modifier(self) -> None:
        from gobby.cli.install import install

        option_names = _option_names(install)
        assert "--falkordb" not in option_names
        assert "--falkordb-password-stdin" in option_names
        assert "--falkordb-password" not in option_names
        assert "--neo4j" not in option_names
        assert "--neo4j-password" not in option_names

        password = _param_by_name(install, "falkordb_password_stdin")
        assert password.help is not None
        assert "FalkorDB" in password.help
        assert "stdin" in password.help

    def test_install_help_omits_removed_graph_flags(self, runner: CliRunner) -> None:
        from gobby.cli.install import install

        result = runner.invoke(install, ["--help"])

        assert result.exit_code == 0
        assert "--falkordb " not in result.output
        assert "--falkordb-password-stdin" in result.output
        assert "--falkordb-password " not in result.output
        assert "--neo4j" not in result.output
        assert "--neo4j-password" not in result.output

    def test_uninstall_exposes_no_graph_target_flags(self) -> None:
        from gobby.cli.uninstall import uninstall

        option_names = _option_names(uninstall)
        assert "--falkordb" not in option_names
        assert "--volumes" not in option_names
        assert "--neo4j" not in option_names

    def test_removed_falkordb_target_is_rejected(self, runner: CliRunner) -> None:
        from gobby.cli.install import install

        result = runner.invoke(install, ["--falkordb"])

        assert result.exit_code == 2
        assert "No such option '--falkordb'" in result.output

    def test_falkordb_password_stdin_rejects_empty_input(self, runner: CliRunner) -> None:
        from gobby.cli.install import install

        result = runner.invoke(
            install,
            ["--falkordb-password-stdin"],
            input="",
        )

        assert result.exit_code == 2
        assert "requires a password on stdin" in result.output

    def test_invalid_falkordb_password_returns_usage_error_without_side_effects(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        from gobby.cli.install import install

        gobby_home = tmp_path / "gobby-home"

        with (
            patch("gobby.cli.installers.falkor.shutil.which") as mock_docker_lookup,
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_subprocess,
            patch("gobby.cli.installers.falkor._update_config") as mock_update_config,
        ):
            result = runner.invoke(
                install,
                ["--falkordb-password-stdin", "--no-interactive"],
                input="has space",
                env={"GOBBY_HOME": str(gobby_home)},
            )

        assert result.exit_code == 2
        assert "FalkorDB password must not contain whitespace" in result.output
        assert "Traceback" not in result.output
        mock_docker_lookup.assert_not_called()
        mock_subprocess.assert_not_called()
        mock_update_config.assert_not_called()
        assert not (gobby_home / "bootstrap.yaml").exists()
        assert not (gobby_home / "hub-postgres.db").exists()
        assert not (gobby_home / "services").exists()
