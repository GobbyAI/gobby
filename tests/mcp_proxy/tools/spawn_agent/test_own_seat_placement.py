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
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.mcp_proxy.tools.spawn_agent._placement import PlacedLaunch, preflight_placement
from gobby.storage.tasks import Task
from gobby.utils.session_context import session_context_for_test
from tests.agents import test_team_epic_dispatch as epic_dispatch
from tests.agents.test_team_epic_dispatch import LEAF_TITLE, epic_team, new_task
from tests.agents.test_team_epic_dispatch import world as world_fixture
from tests.fixtures.team_case import TITLE, TeamWorld
from tests.mcp_proxy.tools.spawn_agent import test_placement as spawn_placement
from tests.mcp_proxy.tools.spawn_agent.test_placement import placed as placed_fixture
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
world = world_fixture
placed_harness = placed_fixture


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


def _restore_real_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the world's launch stub so the real spawn path runs its placement."""
    monkeypatch.setattr(_factory, "spawn_agent_impl", spawn_agent_impl)
    monkeypatch.setattr(_factory, "_load_agent_body", epic_dispatch._REAL_AGENT_BODY)


async def _manager_spawns_developer(
    manager: str, h: spawn_placement._Harness, leaf: Task, placement: dict[str, Any]
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "agent": "developer",
        "task_id": leaf.id,
        "prompt": "Work the leaf",
        "checkout_mode": "worktree",
        "placement": placement,
        "project_path": h.project_path,
    }
    with session_context_for_test(manager):
        reply: dict[str, Any] = await spawn_placement._registry(h).call("spawn_agent", arguments)
    return reply


@pytest.mark.asyncio
async def test_epic_manager_places_its_fresh_seat_into_its_own_team_tab(
    world: TeamWorld,
    placed_harness: spawn_placement._Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An epic Dev Manager's fresh seat for its admitted leaf lands in its own team tab."""
    _restore_real_spawn(monkeypatch)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    team = await epic_team(world, epic)
    h = placed_harness
    tab_title = f"Dev Team - #{epic.seq_num}"
    team_pane = spawn_placement._held_seat(h, tab_title, state="exited")
    split = {
        "workspace": h.workspace.id,
        "tab_title": tab_title,
        "axis": "right",
        "title": "developer",
    }

    result = await _manager_spawns_developer(team.manager, h, leaf, {"split": split})

    assert result["success"] is True, result
    [pane_id] = [
        pane for pane, bound in spawn_placement._panes(h).items() if bound == result["terminal_id"]
    ]
    [pane] = [p for p in h.workspaces.list_panes(h.workspace.id) if p.id == pane_id]
    assert pane.tab_id == team_pane.tab_id


@pytest.mark.asyncio
async def test_epic_manager_fresh_seat_stays_refused_outside_its_team(
    world: TeamWorld,
    placed_harness: spawn_placement._Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another team's tab and an operator tab stay refused for an autonomous Dev Manager."""
    _restore_real_spawn(monkeypatch)
    epic = new_task(world, TITLE, epic=True)
    leaf = new_task(world, LEAF_TITLE, parent=epic)
    other = new_task(world, "Another team's epic", epic=True)
    team = await epic_team(world, epic)
    h = placed_harness
    other_tab = f"Dev Team - #{other.seq_num}"
    spawn_placement._held_seat(h, other_tab, state="exited")
    spawn_placement._held_seat(h, "Operator", state="exited")
    panes = spawn_placement._panes(h)
    terminals = spawn_placement._terminal_states(h)
    for title in (other_tab, "Operator"):
        split = {
            "workspace": h.workspace.id,
            "tab_title": title,
            "axis": "right",
            "title": "developer",
        }
        result = await _manager_spawns_developer(team.manager, h, leaf, {"split": split})
        assert result["success"] is False, (title, result)
        assert result["placement_error"] == "forbidden", (title, result)
    assert h.launches == []
    assert spawn_placement._panes(h) == panes
    assert spawn_placement._terminal_states(h) == terminals
    assert h.events == []
