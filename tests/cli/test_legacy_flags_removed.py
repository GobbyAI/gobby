"""Legacy task list flags are removed after Phase 5."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from gobby.cli import cli

pytestmark = pytest.mark.unit


def test_status_flag_unknown() -> None:
    result = CliRunner().invoke(cli, ["tasks", "list", "--status", "open"])

    assert result.exit_code != 0
    assert "No such option" in result.output
