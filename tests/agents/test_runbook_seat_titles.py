"""Generic placement preserves independent tab and pane titles."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.storage.workspaces import WorkspaceManager
from gobby.terminals.workspace_agent_panes import AgentPaneReserver, AgentPlacement
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID
from tests.terminals.fakes import FakeRuntime, runtime_registry


@pytest.mark.parametrize(
    ("tab_title", "seat_title"),
    [
        pytest.param("Build - #17", "Team Captain", id="task-tab"),
        pytest.param("Operations", "Release Owner", id="static-tab"),
        pytest.param("Exploration - #42", "Investigator", id="custom-tab"),
    ],
)
@pytest.mark.asyncio
async def test_tab_opening_seat_keeps_its_definition_pane_title(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tab_title: str,
    seat_title: str,
) -> None:
    """Reservation preserves the tab title and gives its opener the supplied seat title."""
    db = session_manager.db
    machine_id = f"{TEST_MACHINE_ID_PREFIX}000000000001"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    LocalMachineManager(db).upsert_seen(machine_id, TEST_USER_ID, hostname="local")
    workspaces = WorkspaceManager(db)
    workspace = workspaces.create(machine_id, "seat-titles")[0]
    reserver = AgentPaneReserver(
        workspaces=workspaces,
        terminals=TerminalManager(db),
        registry=runtime_registry(FakeRuntime(backend="tmux"), FakeRuntime(backend="native")),
        sessions=session_manager,
        publish=AsyncMock(),
    )
    placement = AgentPlacement.parse(
        {"tab": {"workspace": workspace.id, "title": tab_title}}
    ).for_seat(seat_title)

    resolved = await reserver.preflight("operator", sample_project["id"], placement)
    reserved = await reserver.reserve(resolved, worktree_id=None)

    tab = next(tab for tab in workspaces.list_tabs(workspace.id) if tab.id == reserved.tab_id)
    pane = next(pane for pane in workspaces.list_panes(workspace.id) if pane.id == reserved.pane_id)
    assert tab.title == tab_title
    assert pane.label == seat_title


def _placement_titles(placement: dict[str, Any]) -> tuple[str | None, str]:
    """Return (pane title, tab title). A tab placement leaves the pane to the session title."""
    if "split" in placement:
        return placement["split"]["title"], placement["split"]["tab_title"]
    return None, placement["tab"]["title"]
