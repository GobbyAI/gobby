"""Tests for FalkorDB CLI flags and legacy Neo4j migration errors."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit

MIGRATION_MESSAGE = "Neo4j has been replaced by FalkorDB. Use --falkordb or --falkordb-password."


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

    def test_uninstall_exposes_falkordb_target_flag(self) -> None:
        from gobby.cli.install import uninstall

        option_names = _option_names(uninstall)
        assert "--falkordb" in option_names
        assert "--neo4j" in option_names

        legacy = _param_by_name(uninstall, "neo4j_flag")
        assert legacy.hidden is True


class TestLegacyNeo4jFlagErrors:
    def test_install_neo4j_password_hidden_flag_fails_with_migration_message(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.cli.install import install

        monkeypatch.setattr(install, "callback", lambda *args, **kwargs: None)

        result = runner.invoke(install, ["--neo4j-password", "secret"])

        assert result.exit_code == 2
        assert MIGRATION_MESSAGE in result.output

    def test_install_neo4j_hidden_flag_fails_with_migration_message(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.cli.install import install

        monkeypatch.setattr(install, "callback", lambda *args, **kwargs: None)

        result = runner.invoke(install, ["--neo4j"])

        assert result.exit_code == 2
        assert MIGRATION_MESSAGE in result.output

    def test_uninstall_neo4j_hidden_flag_fails_with_migration_message(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.cli.install import uninstall

        monkeypatch.setattr(uninstall, "callback", lambda *args, **kwargs: None)

        result = runner.invoke(uninstall, ["--neo4j", "--yes"])

        assert result.exit_code == 2
        assert MIGRATION_MESSAGE in result.output
