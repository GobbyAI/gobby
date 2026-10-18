"""A lane manager's own-lane seat resume is the one autonomous placement admitted.

Placement and the spawn gate decide through one check, so a placed resume passes only
where admission allowed the same resume: a running lane manager, the seat in its lane,
and the seat's stored type. A fresh spawn and a seat in another lane stay refused.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.agents.sandbox import SandboxConfig
from gobby.mcp_proxy.tools.spawn_agent._placement import PlacedLaunch, preflight_placement
from gobby.workflows.state_manager import SessionVariableManager
from tests.terminals.test_workspace_agent_panes import (
    LOCAL_MACHINE_ID,
    _Harness,
    _workspace,
    harness,
)
from tests.workflows.test_spawn_scope_rules import _define_agent, _register, _seat_in_lane

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.mark.asyncio
async def test_placed_resume_is_admitted_only_for_the_lane_managers_own_seat(
    harness: _Harness,
) -> None:
    h = harness
    workspace = _workspace(h)
    _define_agent(h.db, "lane-manager", None)
    root = _register(h.sessions, h.project_id, "placed-resume-root")
    manager = _seat_in_lane(h.db, h.sessions, root, "lane-manager", "lane-manager", "7")
    own_seat = _seat_in_lane(h.db, h.sessions, root, "seat-own-lane", "developer", "7")
    other_seat = _seat_in_lane(h.db, h.sessions, root, "seat-other-lane", "developer", "8")
    for seat in (own_seat, other_seat):
        SessionVariableManager(h.db).merge_variables(seat.id, {"_agent_type": "developer"})
    placement = {"tab": {"workspace": workspace.id, "title": "seat"}}

    async def place(resume_session_id: str | None) -> PlacedLaunch | dict[str, Any] | None:
        return await preflight_placement(
            placement,
            reserver=h.reserver,
            sandbox_config=SandboxConfig(enabled=True, backend="srt"),
            parent_session_id=manager.id,
            machine_id=LOCAL_MACHINE_ID,
            project_id=h.project_id,
            project_context_authoritative=True,
            session_manager=h.sessions,
            agent_name="developer",
            resume_session_id=resume_session_id,
        )

    admitted = await place(own_seat.id)
    assert isinstance(admitted, PlacedLaunch), admitted
    assert admitted.resolved.workspace_id == workspace.id

    fresh = await place(None)
    other_lane = await place(other_seat.id)
    assert isinstance(fresh, dict) and fresh["placement_error"] == "forbidden"
    assert isinstance(other_lane, dict) and other_lane["placement_error"] == "forbidden"
    assert h.events == [] and h.minted == []
