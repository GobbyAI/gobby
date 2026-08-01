"""Removal contracts for the retired export/import CLI surface."""

import pytest
from click.testing import CliRunner

from gobby.cli import cli

pytestmark = pytest.mark.unit


def test_export_import_commands_are_absent_from_root_help() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "export" not in cli.commands
    assert "import" not in cli.commands
    assert "  export " not in result.output
    assert "  import " not in result.output
