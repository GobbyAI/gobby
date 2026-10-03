"""Isolated test daemons shed the parent agent's identity (#21175)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.agents.constants import ALL_TERMINAL_ENV_VARS
from tests.e2e.conftest import prepare_daemon_env

pytestmark = pytest.mark.e2e


def test_nested_daemon_discards_parent_agent_identity(tmp_path: Path) -> None:
    inherited_names = [
        *ALL_TERMINAL_ENV_VARS,
        "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
        "GOBBY_MACHINE_ID",
        "GOBBY_DAEMON_PORT",
    ]
    inherited = dict.fromkeys(inherited_names, "parent-only")
    inherited.update({"TMPDIR": str(tmp_path), "GOBBY_TEST_PROTECT": "0"})
    prepared = prepare_daemon_env(inherited, home_dir=tmp_path)

    assert not set(inherited_names).intersection(prepared)
    assert prepared["HOME"] == str(tmp_path)
    assert prepared["TMPDIR"] == str(tmp_path)
    assert prepared["GOBBY_TEST_PROTECT"] == "1"
    assert inherited["GOBBY_AGENT_API_TOKEN"] == "parent-only"
