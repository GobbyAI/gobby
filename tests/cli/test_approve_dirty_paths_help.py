"""The approve-dirty-paths help states the real admission rule, not an operator-only ban."""

import pytest
from click.testing import CliRunner

from gobby.cli.sessions_dirty_paths import approve_dirty_paths

pytestmark = pytest.mark.unit


def test_help_states_that_installed_definitions_grant_the_exemption() -> None:
    result = CliRunner().invoke(approve_dirty_paths, ["--help"])

    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "Operator only" not in text
    assert "refused unless their installed definition exempts them" in text
