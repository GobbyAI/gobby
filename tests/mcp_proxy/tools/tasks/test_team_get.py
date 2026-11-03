"""The lead reads its team through the gobby-tasks registry: get_team beside team_progress."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks._team_progress import (
    create_team_progress_registry,
    team_binding_payload,
)
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
async def test_lead_reads_its_team_binding_beside_team_progress(world: TeamWorld) -> None:
    task = world.case.task(TITLE)
    team = await world.admit(world.child(task), task)

    assert team_binding_payload(world.case.db, team.team_id) == asdict(team)
    assert team_binding_payload(world.case.db, "00000000-0000-4000-8000-000000000000") == {
        "error": "Team 00000000-0000-4000-8000-000000000000 not found",
        "error_code": "team_not_found",
    }
    registry = create_team_progress_registry(MagicMock())
    assert "team_progress" in registry
    assert "get_team" in registry
