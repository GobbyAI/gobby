"""A resumed seat returns to its lane tab instead of launching unplaced (#24480)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.agents import spawn_in_doubt_owner
from gobby.mcp_proxy.tools.spawn_agent import _implementation as impl
from gobby.mcp_proxy.tools.spawn_agent._resume import ResumeSeatGone, resumed_seat_plan
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.workspace_layout import WorkspaceNotFoundError
from tests.mcp_proxy.tools.spawn_agent.test_placement import (
    LOCAL_MACHINE_ID,
    SEAT,
    _build,
    _Harness,
    _held_seat,
    _spawn,
    _tabs,
)
from tests.terminals.fakes import FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]


@pytest.fixture(autouse=True)
def _local_machine_identity(
    monkeypatch: pytest.MonkeyPatch, _mock_spawn_machine_id: None
) -> Iterator[None]:
    # Runs after the package conftest's alternate machine id, so this one wins.
    monkeypatch.setattr(impl, "get_machine_id", lambda: LOCAL_MACHINE_ID)
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture(autouse=True)
def _instant_owner_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_wait(delay: float) -> None:
        del delay
        await asyncio.sleep(0)

    monkeypatch.setattr(spawn_in_doubt_owner, "_sleep", no_wait)


@pytest.fixture
def placed(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _Harness:
    return _build(
        db=temp_db,
        project=sample_project,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        runtimes=(FakeRuntime(backend="native"),),
    )


def _ended_run(
    h: _Harness,
    metadata: dict[str, Any] | None,
    *,
    terminal_id: str | None = None,
) -> AgentRun:
    """A cancelled developer run with ``metadata`` as its resume payload."""
    run = h.runs.create(
        parent_session_id=h.parent_id,
        provider="claude",
        prompt="Run the seat",
        agent_name="developer",
        resume_metadata_json=metadata,
    )
    if terminal_id is not None:
        h.db.execute("UPDATE agent_runs SET terminal_id = %s WHERE id = %s", (terminal_id, run.id))
    h.runs.start(run.id)
    ended = h.runs.cancel(run.id, terminal_reason="daemon_stop")
    assert ended is not None
    return ended


def _seat_session(h: _Harness, run: AgentRun, status: str = "expired") -> Session:
    """The seat session whose last run is ``run``, held in ``status``, ready to resume."""
    child = h.sessions.register(
        external_id=f"resume-seat-{uuid.uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=h.project_id,
        parent_session_id=h.parent_id,
    )
    h.db.execute("UPDATE sessions SET agent_run_id = %s WHERE id = %s", (run.id, child.id))
    h.db.execute("UPDATE agent_runs SET child_session_id = %s WHERE id = %s", (child.id, run.id))
    h.sessions.update(child.id, status=status)
    session = h.sessions.get(child.id)
    assert session is not None
    return session


def _split_snapshot(
    h: _Harness, *, beside: str, tab_id: str | None, axis: str = "vertical"
) -> dict[str, Any]:
    return {
        "placement": {
            "kind": "split",
            "workspace_id": h.workspace.id,
            "title": SEAT,
            "beside_pane_id": beside,
            "tab_id": tab_id,
            "axis": axis,
        }
    }


def _tab_snapshot(h: _Harness) -> dict[str, Any]:
    return {
        "placement": {
            "kind": "tab",
            "workspace_id": h.workspace.id,
            "title": SEAT,
            "beside_pane_id": None,
            "tab_id": None,
            "axis": None,
        }
    }


def _placement(h: _Harness, run: AgentRun) -> dict[str, Any] | None:
    return resumed_seat_plan(_seat_session(h, run), h.runs, h.workspaces)[0]


def test_previous_runs_own_pane_comes_first(placed: _Harness) -> None:
    h = placed
    lane = _held_seat(h, "Lane 1", state="exited")
    elsewhere = _held_seat(h, "Lane 2", state="exited")
    run = _ended_run(
        h,
        _split_snapshot(h, beside=elsewhere.id, tab_id=elsewhere.tab_id),
        terminal_id=lane.terminal_id,
    )

    assert _placement(h, run) == {"split": {"pane": lane.id, "axis": "down", "title": SEAT}}


def test_own_pane_without_any_recorded_title_uses_the_session_title(placed: _Harness) -> None:
    h = placed
    lane = _held_seat(h, "Lane 1", state="exited")
    session = _seat_session(h, _ended_run(h, None, terminal_id=lane.terminal_id))
    h.sessions.update(session.id, title="Lane 1 developer")
    titled = h.sessions.get(session.id)
    assert titled is not None

    assert resumed_seat_plan(titled, h.runs, h.workspaces)[0] == {
        "split": {"pane": lane.id, "axis": "down", "title": "Lane 1 developer"}
    }


def test_snapshot_found_along_the_resume_chain(placed: _Harness) -> None:
    h = placed
    lane = _held_seat(h, "Lane 1", state="exited")
    placed_run = _ended_run(
        h, _split_snapshot(h, beside=lane.id, tab_id=lane.tab_id, axis="horizontal")
    )
    unplaced_resume = _ended_run(h, {"resume_previous_run_id": placed_run.id})

    assert _placement(h, unplaced_resume) == {
        "split": {"pane": lane.id, "axis": "right", "title": SEAT}
    }


def test_swept_beside_pane_falls_back_to_a_pane_in_its_tab(placed: _Harness) -> None:
    h = placed
    lane = _held_seat(h, "Lane 1", state="exited")
    run = _ended_run(h, _split_snapshot(h, beside=str(uuid.uuid4()), tab_id=lane.tab_id))

    assert _placement(h, run) == {"split": {"pane": lane.id, "axis": "down", "title": SEAT}}


def test_balanced_split_keeps_its_viewport(placed: _Harness) -> None:
    h = placed
    lane = _held_seat(h, "Lane 1", state="exited")
    metadata = _split_snapshot(h, beside=lane.id, tab_id=lane.tab_id, axis="balanced")
    metadata["placement"] |= {"columns": 319, "rows": 55}
    run = _ended_run(h, metadata)

    assert _placement(h, run) == {
        "split": {"pane": lane.id, "axis": "balanced", "title": SEAT, "columns": 319, "rows": 55}
    }


def test_tab_seat_splits_into_its_surviving_tab(placed: _Harness) -> None:
    h = placed
    seat_tab = _held_seat(h, SEAT, state="exited")
    run = _ended_run(h, _tab_snapshot(h))

    assert _placement(h, run) == {"split": {"pane": seat_tab.id, "axis": "down", "title": SEAT}}


def test_tab_seat_reopens_its_tab_when_gone(placed: _Harness) -> None:
    h = placed
    run = _ended_run(h, _tab_snapshot(h))

    assert _placement(h, run) == {"tab": {"workspace": h.workspace.id, "title": SEAT}}


def test_never_placed_seat_stays_unplaced(placed: _Harness) -> None:
    h = placed
    run = _ended_run(h, {"provider": "claude"})

    assert _placement(h, run) is None


def test_seat_whose_pane_and_tab_are_gone_reopens_a_fresh_tab(placed: _Harness) -> None:
    h = placed
    run = _ended_run(h, _split_snapshot(h, beside=str(uuid.uuid4()), tab_id=str(uuid.uuid4())))

    assert _placement(h, run) == {"tab": {"workspace": h.workspace.id, "title": SEAT}}


def test_seat_with_no_recorded_workspace_is_refused(placed: _Harness) -> None:
    h = placed
    run = _ended_run(
        h,
        {
            "placement": {
                "kind": "split",
                "title": SEAT,
                "beside_pane_id": str(uuid.uuid4()),
                "tab_id": str(uuid.uuid4()),
                "axis": "vertical",
            }
        },
    )

    with pytest.raises(ResumeSeatGone, match="pass placement"):
        _placement(h, run)


async def test_resumed_seat_launches_in_its_lane_tab(placed: _Harness) -> None:
    h = placed
    lane = _held_seat(h, "Lane 1", state="exited")
    session = _seat_session(
        h, _ended_run(h, _split_snapshot(h, beside=str(uuid.uuid4()), tab_id=lane.tab_id))
    )

    reply = await _spawn(h, None, resume_session_id=session.id)
    await asyncio.gather(*list(impl._spawn_background_tasks.values()))

    assert reply["success"] is True, reply
    run = h.runs.get(reply["run_id"])
    assert run is not None and run.resume_metadata_json is not None
    assert run.resume_metadata_json["placement"]["tab_id"] == lane.tab_id
    assert run.terminal_id is not None
    pane = h.workspaces.get_pane_for_terminal(run.terminal_id)
    assert pane is not None and pane.tab_id == lane.tab_id
    assert _tabs(h) == {lane.tab_id}


async def test_resumed_paused_seat_without_a_live_pane_rebinds_and_launches(
    placed: _Harness,
) -> None:
    h = placed
    lane = _held_seat(h, "Lane 1", state="exited")
    session = _seat_session(
        h,
        _ended_run(h, _split_snapshot(h, beside=str(uuid.uuid4()), tab_id=lane.tab_id)),
        status="paused",
    )

    reply = await _spawn(h, None, resume_session_id=session.id)
    await asyncio.gather(*list(impl._spawn_background_tasks.values()))

    assert reply["success"] is True, reply
    run = h.runs.get(reply["run_id"])
    assert run is not None and run.terminal_id is not None
    pane = h.workspaces.get_pane_for_terminal(run.terminal_id)
    assert pane is not None and pane.tab_id == lane.tab_id


async def test_resumed_seat_whose_pane_and_tab_are_gone_reopens_its_tab(placed: _Harness) -> None:
    h = placed
    session = _seat_session(
        h,
        _ended_run(h, _split_snapshot(h, beside=str(uuid.uuid4()), tab_id=str(uuid.uuid4()))),
    )

    reply = await _spawn(h, None, resume_session_id=session.id)
    await asyncio.gather(*list(impl._spawn_background_tasks.values()))

    assert reply["success"] is True, reply
    run = h.runs.get(reply["run_id"])
    assert run is not None and run.terminal_id is not None
    pane = h.workspaces.get_pane_for_terminal(run.terminal_id)
    assert pane is not None
    tab = next(tab for tab in h.workspaces.list_tabs(h.workspace.id) if tab.id == pane.tab_id)
    assert tab.title == SEAT


async def test_resumed_seat_replaces_its_empty_recorded_tab(placed: _Harness) -> None:
    h = placed
    lane = _held_seat(h, SEAT, state="exited")
    h.db.execute("DELETE FROM workspace_panes WHERE id = %s", (lane.id,))
    session = _seat_session(
        h, _ended_run(h, _split_snapshot(h, beside=str(uuid.uuid4()), tab_id=lane.tab_id))
    )

    reply = await _spawn(h, None, resume_session_id=session.id)
    await asyncio.gather(*list(impl._spawn_background_tasks.values()))

    assert reply["success"] is True, reply
    run = h.runs.get(reply["run_id"])
    assert run is not None and run.terminal_id is not None
    pane = h.workspaces.get_pane_for_terminal(run.terminal_id)
    assert pane is not None and pane.tab_id != lane.tab_id
    assert [tab.title for tab in h.workspaces.list_tabs(h.workspace.id)] == [SEAT]


async def test_busy_planned_tab_refuses_the_resume_and_keeps_its_pane(
    placed: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = placed
    lane = _held_seat(h, SEAT, state="exited")
    h.db.execute("DELETE FROM workspace_panes WHERE id = %s", (lane.id,))
    session = _seat_session(
        h, _ended_run(h, _split_snapshot(h, beside=str(uuid.uuid4()), tab_id=lane.tab_id))
    )
    plan = resumed_seat_plan
    filled: list[str] = []

    def plan_then_fill_tab(*args: Any) -> Any:
        planned = plan(*args)
        # A pane lands in the planned tab between plan and launch: move one in from a scratch tab.
        scratch = h.workspaces.create_tab(
            h.workspace.id, pane_id=str(uuid.uuid4()), project_id=h.project_id, title="scratch"
        )
        h.db.execute(
            "UPDATE workspace_panes SET tab_id = %s WHERE id = %s",
            (lane.tab_id, scratch.panes[0].id),
        )
        h.db.execute("DELETE FROM workspace_tabs WHERE id = %s", (scratch.tabs[0].id,))
        filled.append(scratch.panes[0].id)
        return planned

    monkeypatch.setattr(impl, "resumed_seat_plan", plan_then_fill_tab)

    reply = await _spawn(h, None, resume_session_id=session.id)
    await asyncio.gather(*list(impl._spawn_background_tasks.values()))

    assert reply["success"] is False, reply
    assert reply["placement_error"] == "busy", reply
    assert _tabs(h) == {lane.tab_id}
    assert [
        pane.id for pane in h.workspaces.list_panes(h.workspace.id) if pane.tab_id == lane.tab_id
    ] == filled


async def test_resume_whose_empty_tab_is_gone_at_launch_still_launches(
    placed: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = placed
    lane = _held_seat(h, SEAT, state="exited")
    h.db.execute("DELETE FROM workspace_panes WHERE id = %s", (lane.id,))
    session = _seat_session(
        h, _ended_run(h, _split_snapshot(h, beside=str(uuid.uuid4()), tab_id=lane.tab_id))
    )
    plan = resumed_seat_plan

    def plan_then_lose_tab(*args: Any) -> Any:
        planned = plan(*args)
        h.db.execute("DELETE FROM workspace_tabs WHERE id = %s", (lane.tab_id,))
        return planned

    monkeypatch.setattr(impl, "resumed_seat_plan", plan_then_lose_tab)

    reply = await _spawn(h, None, resume_session_id=session.id)
    await asyncio.gather(*list(impl._spawn_background_tasks.values()))

    assert reply["success"] is True, reply
    run = h.runs.get(reply["run_id"])
    assert run is not None and run.terminal_id is not None
    pane = h.workspaces.get_pane_for_terminal(run.terminal_id)
    assert pane is not None and pane.tab_id != lane.tab_id
    assert [tab.title for tab in h.workspaces.list_tabs(h.workspace.id)] == [SEAT]


async def test_rehome_during_close_refuses_the_resume_and_keeps_the_tab(
    placed: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planned tab rehomed into another workspace while it closes refuses the resume.

    The store raises not_found when the tab moves between its home read and its lock, and
    the row remains. The resume must refuse, not launch beside the rehomed tab.
    """
    h = placed
    lane = _held_seat(h, SEAT, state="exited")
    h.db.execute("DELETE FROM workspace_panes WHERE id = %s", (lane.id,))
    session = _seat_session(
        h, _ended_run(h, _split_snapshot(h, beside=str(uuid.uuid4()), tab_id=lane.tab_id))
    )
    other, _ = h.workspaces.create(LOCAL_MACHINE_ID, "rehome-target")
    plan = resumed_seat_plan
    filled: list[str] = []

    def plan_then_fill_tab(*args: Any) -> Any:
        planned = plan(*args)
        # A pane lands in the planned tab between plan and close: move one in from a scratch tab.
        scratch = h.workspaces.create_tab(
            h.workspace.id, pane_id=str(uuid.uuid4()), project_id=h.project_id, title="scratch"
        )
        h.db.execute(
            "UPDATE workspace_panes SET tab_id = %s WHERE id = %s",
            (lane.tab_id, scratch.panes[0].id),
        )
        h.db.execute("DELETE FROM workspace_tabs WHERE id = %s", (scratch.tabs[0].id,))
        filled.append(scratch.panes[0].id)
        return planned

    def close_after_rehome(tab_id: str, **_: Any) -> Any:
        # The store's race: the tab moves between close's home read and its lock.
        h.workspaces.move_tab(tab_id, workspace_id=other.id, position=0)
        raise WorkspaceNotFoundError("A tab moved to another workspace concurrently; retry")

    monkeypatch.setattr(impl, "resumed_seat_plan", plan_then_fill_tab)
    monkeypatch.setattr(h.workspaces, "close_tab", close_after_rehome)

    reply = await _spawn(h, None, resume_session_id=session.id)
    await asyncio.gather(*list(impl._spawn_background_tasks.values()))

    assert reply["success"] is False, reply
    assert reply["placement_error"] == "not_found", reply
    assert h.launches == []
    assert _tabs(h) == set()
    assert [tab.id for tab in h.workspaces.list_tabs(other.id)] == [lane.tab_id]
    assert [pane.id for pane in h.workspaces.list_panes(other.id)] == filled
