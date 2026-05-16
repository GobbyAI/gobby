"""Tests for top-level CLI version handling."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli

pytestmark = pytest.mark.unit


def test_cli_version_uses_version_utility() -> None:
    with patch("gobby.cli.get_version", return_value="9.8.7") as mock_get_version:
        result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert result.output == "gobby, version 9.8.7\n"
    mock_get_version.assert_called_once_with()
