"""A team manager's own-team seat resume is the one autonomous placement admitted.

Placement and the spawn gate decide through one check, so a placed resume passes only
where admission allowed the same resume: a running team manager, a seat of its admitted
team, and the seat's stored type. A fresh spawn and another team's seat stay refused.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.mcp_proxy.tools.spawn_agent._placement import PlacedLaunch, preflight_placement
from tests.terminals.test_workspace_agent_panes import (
    LOCAL_MACHINE_ID,
    _Harness,
    _workspace,
)
from tests.terminals.test_workspace_agent_panes import harness as workspace_fixture
from tests.workflows.test_spawn_scope_rules import _define_agent, _Teams
from tests.workflows.test_spawn_scope_rules import teams as teams_fixture

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]
harness = workspace_fixture
teams = teams_fixture


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.mark.asyncio
async def test_placed_resume_is_admitted_only_for_the_team_managers_own_seat(
    harness: _Harness, teams: _Teams
) -> None:
    h = harness
    workspace = _workspace(h)
    _define_agent(h.db, "dev-manager", None, variables={"team_manager": True})
    placement = {"tab": {"workspace": workspace.id, "title": "seat"}}

    async def place(resume_session_id: str | None) -> PlacedLaunch | dict[str, Any] | None:
        return await preflight_placement(
            placement,
            reserver=h.reserver,
            sandbox_config=SandboxConfig(enabled=True, backend="srt"),
            parent_session_id=teams.manager.id,
            machine_id=LOCAL_MACHINE_ID,
            project_id=h.project_id,
            project_context_authoritative=True,
            session_manager=h.sessions,
            agent_name="developer",
            resume_session_id=resume_session_id,
        )

    admitted = await place(teams.seats["developer"].id)
    assert isinstance(admitted, PlacedLaunch), admitted
    assert admitted.resolved.workspace_id == workspace.id

    fresh = await place(None)
    other_team = await place(teams.outsider.id)
    assert isinstance(fresh, dict) and fresh["placement_error"] == "forbidden"
    assert isinstance(other_team, dict) and other_team["placement_error"] == "forbidden"
    assert h.events == [] and h.minted == []
