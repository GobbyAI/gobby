"""A release seat uses a public batch path without opening daemon credentials.

The adapter probes the existing CLI initially. If the supported surface moves,
adapt only _seat_batch; keep the seat boundary and behavioral assertions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from click.testing import CliRunner, Result

from gobby.cli.release import release
from gobby.releases.window import acquire_window
from gobby.storage.hub.protocol import HubDatabase
from tests.fixtures.landing_case import LandingCase, make_landing_case
from tests.fixtures.release_batch_case import Clock, Fakes

pytestmark = pytest.mark.integration


def _seat_batch(owner: str, token: str, generation: int, base: str) -> Result:
    return CliRunner().invoke(
        release,
        ["batch", "--token", token, "--generation", str(generation), "--base-sha", base],
        env={"GOBBY_SESSION_ID": owner, "DATABASE_URL": None},
    )


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


@pytest.mark.parametrize("invalid", [None, "foreign-token", "wrong-generation"])
def test_release_seat_batch_needs_no_local_database_credentials(
    case: LandingCase, monkeypatch: pytest.MonkeyPatch, invalid: str | None
) -> None:
    import asyncio

    owner = case.session("release manager")
    candidate = case.direct({"src/gobby/tasks/a.py": "A = 1\n"})
    clock = Clock(datetime.now(UTC))
    lease = asyncio.run(
        acquire_window(case.db, project_id=case.project_id, owner_session_id=owner, now=clock.now)
    )
    fakes = Fakes(case, clock)
    monkeypatch.chdir(case.repo)
    monkeypatch.setattr("gobby.cli.release.get_project_context", lambda: {"id": case.project_id})
    # Daemon-side check/activation ports are isolated; seat-side DB access is denied.
    monkeypatch.setattr("gobby.cli.release._environment", lambda *_args: fakes.env())
    denied = Mock(side_effect=PermissionError("seat cannot read bootstrap.yaml"))
    monkeypatch.setattr("gobby.cli.release.init_local_storage", denied)

    result = _seat_batch(
        owner,
        "foreign" if invalid == "foreign-token" else lease.token,
        lease.generation + (invalid == "wrong-generation"),
        case.base,
    )

    assert denied.call_count == 0, "public batch path tried to open daemon bootstrap/DB credentials"
    assert "database_url" not in result.output.lower()
    assert "postgresql://" not in result.output
    if invalid is None:
        assert result.exit_code == 0, (result.output, result.exception)
        assert candidate in result.output
        assert '"succeeded"' in result.output
    else:
        assert result.exit_code != 0
        assert "release_window_" in result.output
        assert not fakes.checks
