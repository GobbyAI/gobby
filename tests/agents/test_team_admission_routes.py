"""Admission refusals point the caller at the route that admits its team."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.agents import team_lifecycle
from gobby.agents.team_admission_records import TeamAdmissionRefused
from gobby.storage.hub.protocol import HubDatabase
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TITLE, TeamWorld, make_team_world

pytestmark = pytest.mark.unit


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    return make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )


@pytest.mark.asyncio
async def test_admit_team_refusal_names_run_pipeline_as_the_route(world: TeamWorld) -> None:
    task = world.case.task(TITLE)

    with pytest.raises(TeamAdmissionRefused) as refused:
        await team_lifecycle.admit_team(
            world.case.db,
            caller_session_id=world.case.creator,
            task=f"#{task.seq_num}",
        )

    assert refused.value.code == "team_caller_invalid"
    assert "run_pipeline" in str(refused.value)
