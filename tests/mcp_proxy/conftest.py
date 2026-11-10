"""Stdio proxy fixtures: an unspawned CLI session bound to its CLI process by a real record."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.fleet_health.state import fleet_state_path
from gobby.utils.session_credential_record import STORE_DIR, cli_pid_key, record_path
from tests.fixtures.fleet_state import keep_fleet_state_fresh

BOUND_CLI_PID = 4242
BOUND_SESSION_ID = "11111111-2222-4333-8444-555555555555"
BOUND_TOKEN = "cli-bound-session-token"
_MANAGED_ENV_NAMES = (
    "GOBBY_SESSION_ID",
    "GOBBY_AGENT_RUN_ID",
    "GOBBY_AGENT_API_TOKEN",
    "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
)


@pytest.fixture(autouse=True)
def bound_cli_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Every proxy here is an unspawned CLI whose session its SessionStart bound.

    The managed identity is cleared so a developer's own agent environment cannot turn an
    unspawned fixture into a managed one. Tests that need another record or none patch the
    same seam again, which takes precedence over this fixture.
    """
    for name in _MANAGED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "gobby-home"
    (home / STORE_DIR).mkdir(mode=0o700, parents=True)
    path = record_path(home, cli_pid_key(BOUND_CLI_PID))
    path.write_text(
        json.dumps({"token": BOUND_TOKEN, "session_id": BOUND_SESSION_ID, "project_id": ""}),
        encoding="utf-8",
    )
    path.chmod(0o600)
    monkeypatch.setenv("GOBBY_HOME", str(home))
    # The root fixture's healthy fleet state lives under the previous home; the fleet gate
    # reads this one, so keep a healthy state fresh here too.
    with (
        keep_fleet_state_fresh(fleet_state_path()),
        patch(
            "gobby.utils.session_credential_record.stable_cli_pid",
            return_value=BOUND_CLI_PID,
        ),
    ):
        yield
