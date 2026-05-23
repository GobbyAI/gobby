"""Tests for FalkorDB CLI flags and legacy Neo4j migration errors."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit

MIGRATION_MESSAGE = "--neo4j / --neo4j-password has been removed in 0.4.0."


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
    def test_install_exposes_falkordb_target_and_password_flags(self) -> None:
        from gobby.cli.install import install

        option_names = _option_names(install)
        assert "--falkordb" in option_names
        assert "--falkordb-password" in option_names

        password = _param_by_name(install, "falkordb_password")
        assert password.help is not None
        assert "FalkorDB" in password.help
        assert "reused from existing config" in password.help

    def test_install_help_hides_legacy_neo4j_flags(self, runner: CliRunner) -> None:
        from gobby.cli.install import install

        result = runner.invoke(install, ["--help"])

        assert result.exit_code == 0
        assert "--falkordb" in result.output
        assert "--falkordb-password" in result.output
        assert "--neo4j" not in result.output
        assert "--neo4j-password" not in result.output

    def test_uninstall_exposes_falkordb_target_flag(self) -> None:
        from gobby.cli.install import uninstall

        option_names = _option_names(uninstall)
        assert "--falkordb" in option_names
        assert "--neo4j" in option_names

        legacy = _param_by_name(uninstall, "neo4j_flag")
        assert legacy.hidden is True

    def test_falkordb_target_runs_only_falkordb_service(self, runner: CliRunner) -> None:
        from gobby.cli.install import install, install_falkordb

        with (
            patch("gobby.cli.install._run_falkordb_install") as mock_falkordb,
            patch("gobby.cli.install._echo_install_summary", return_value=True) as mock_summary,
            patch("gobby.cli.install._ensure_daemon_config") as mock_config,
            patch("gobby.cli.install.run_daemon_setup") as mock_setup,
            patch("gobby.cli.install._run_standard_cli_install") as mock_cli,
            patch("gobby.cli.install._run_git_hooks_install") as mock_hooks,
            patch("gobby.cli.install._run_embedding_install") as mock_embedding,
            patch("gobby.cli.install._run_qdrant_install") as mock_qdrant,
            patch("gobby.cli.install._run_voice_install") as mock_voice,
        ):
            result = runner.invoke(
                install,
                ["--falkordb", "--falkordb-password", "secret"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        mock_falkordb.assert_called_once()
        assert mock_falkordb.call_args.args[0] is install_falkordb
        assert mock_falkordb.call_args.args[1] == "secret"
        mock_summary.assert_called_once()
        mock_config.assert_not_called()
        mock_setup.assert_not_called()
        mock_cli.assert_not_called()
        mock_hooks.assert_not_called()
        mock_embedding.assert_not_called()
        mock_qdrant.assert_not_called()
        mock_voice.assert_not_called()

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
            patch("gobby.cli.installers.falkor._write_bootstrap_password") as mock_bootstrap_write,
        ):
            result = runner.invoke(
                install,
                ["--falkordb", "--falkordb-password", "has space", "--no-interactive"],
                env={"GOBBY_HOME": str(gobby_home)},
            )

        assert result.exit_code == 2
        assert "FalkorDB password must not contain whitespace" in result.output
        assert "Traceback" not in result.output
        mock_docker_lookup.assert_not_called()
        mock_subprocess.assert_not_called()
        mock_update_config.assert_not_called()
        mock_bootstrap_write.assert_not_called()
        assert not (gobby_home / "bootstrap.yaml").exists()
        assert not (gobby_home / "gobby-hub.db").exists()
        assert not (gobby_home / "services").exists()


class TestLegacyNeo4jFlagErrors:
    def test_install_neo4j_password_hidden_flag_fails_with_migration_message(
        self, runner: CliRunner
    ) -> None:
        from gobby.cli.install import install

        result = runner.invoke(install, ["--neo4j-password", "secret"])

        assert result.exit_code == 2
        assert MIGRATION_MESSAGE in result.output
        assert "gobby install --falkordb" in result.output
        assert "gobby uninstall --falkordb" in result.output

    def test_install_neo4j_hidden_flag_fails_with_migration_message(
        self, runner: CliRunner
    ) -> None:
        from gobby.cli.install import install

        result = runner.invoke(install, ["--neo4j"])

        assert result.exit_code == 2
        assert MIGRATION_MESSAGE in result.output

    def test_uninstall_neo4j_hidden_flag_fails_with_migration_message(
        self, runner: CliRunner
    ) -> None:
        from gobby.cli.install import uninstall

        result = runner.invoke(uninstall, ["--neo4j", "--yes"])

        assert result.exit_code == 2
        assert MIGRATION_MESSAGE in result.output
