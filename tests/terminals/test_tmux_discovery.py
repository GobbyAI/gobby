"""The tmux discovery sweep mirrors live panes into ``terminals`` rows (#21190)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gobby.agents.tmux.session_manager import TmuxPaneInfo
from gobby.config.tmux import TmuxConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager, tmux_locator_key
from gobby.terminals.tmux_discovery import (
    PaneOwner,
    _project_for_path,
    pane_owners,
    socket_path_for,
    sweep_tmux_terminals,
)
from tests.storage.test_terminals import LOCAL_MACHINE_ID

pytestmark = pytest.mark.unit

DEFAULT_SOCKET = socket_path_for(TmuxConfig(socket_name=""))
GOBBY_SOCKET = socket_path_for(TmuxConfig(socket_name="gobby"))


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


class FakeTmux:
    """A ``TmuxSessionManager`` stand-in that answers ``list_panes`` from memory."""

    def __init__(self, socket_name: str, panes: list[TmuxPaneInfo] | None) -> None:
        self.config = TmuxConfig(socket_name=socket_name)
        self.panes = panes

    async def list_panes(self) -> list[TmuxPaneInfo] | None:
        return self.panes


def pane(
    socket_path: str,
    pane_id: str,
    *,
    session_name: str = "0",
    window_name: str | None = "zsh",
    pane_title: str | None = "MBP.local",
    pane_command: str | None = "zsh",
    pane_path: str | None = "/nowhere",
    pane_dead: bool = False,
    server_pid: int = 6051,
) -> TmuxPaneInfo:
    return TmuxPaneInfo(
        socket_path=socket_path,
        server_pid=server_pid,
        server_start_time=1787385464,
        session_name=session_name,
        window_id="@1",
        window_name=window_name,
        pane_id=pane_id,
        pane_pid=100,
        pane_title=pane_title,
        pane_dead=pane_dead,
        pane_command=pane_command,
        pane_path=pane_path,
    )


def key_of(info: TmuxPaneInfo) -> str:
    return tmux_locator_key(
        socket_path=info.socket_path,
        server_pid=info.server_pid,
        server_start_time=info.server_start_time,
        pane_id=info.pane_id,
    )


def live_external(manager: TerminalManager) -> dict[str, Any]:
    return {
        row.locator_key: row
        for row in manager.list_live_by_machine(LOCAL_MACHINE_ID)
        if row.ownership == "external" and row.locator_key
    }


@pytest.mark.asyncio
async def test_sweep_mirrors_live_panes_on_both_sockets_idempotently(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = TerminalManager(temp_db)
    user_pane = pane(DEFAULT_SOCKET, "%1", session_name="75", window_name="tail")
    agent_pane = pane(
        GOBBY_SOCKET, "%5", session_name="gobby-done", window_name=None, pane_title="t"
    )
    dead_pane = pane(DEFAULT_SOCKET, "%9", pane_dead=True)
    tmux = [FakeTmux("", [user_pane, dead_pane]), FakeTmux("gobby", [agent_pane])]

    for _ in range(2):
        seen = await sweep_tmux_terminals(
            manager,
            tmux,
            machine_id=LOCAL_MACHINE_ID,
            owners={},
            fallback_project_id=sample_project["id"],
        )

    assert set(seen) == {key_of(user_pane), key_of(agent_pane)}
    rows = live_external(manager)
    assert set(rows) == {key_of(user_pane), key_of(agent_pane)}
    user_row = rows[key_of(user_pane)]
    assert (user_row.session_name, user_row.window_id, user_row.title) == ("75", "@1", "tail")
    assert user_row.project_id == sample_project["id"]
    assert user_row.backend == "tmux"
    assert rows[key_of(agent_pane)].title == "t"


@pytest.mark.asyncio
async def test_sweep_leaves_gobby_owned_rows_to_their_lifecycle(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = TerminalManager(temp_db)
    promoted = pane(GOBBY_SOCKET, "%2", session_name="gobby-live", window_name="renamed")
    live = manager.create_pending(
        terminal_id=str(uuid.uuid4()),
        project_id=sample_project["id"],
        backend="tmux",
        ownership="gobby",
        spawn_key="gobby-live",
        title="agent title",
    )
    manager.promote_to_live(
        live.id,
        locator={
            "socket_path": promoted.socket_path,
            "server_pid": promoted.server_pid,
            "server_start_time": promoted.server_start_time,
            "pane_id": promoted.pane_id,
        },
        locator_key=key_of(promoted),
        session_name="gobby-live",
        title="agent title",
    )
    manager.create_pending(
        terminal_id=str(uuid.uuid4()),
        project_id=sample_project["id"],
        backend="tmux",
        ownership="gobby",
        spawn_key="gobby-spawning",
    )
    temp_db.execute(
        "UPDATE terminals SET session_name = %s WHERE spawn_key = %s",
        ("gobby-spawning", "gobby-spawning"),
    )
    spawning = pane(GOBBY_SOCKET, "%3", session_name="gobby-spawning")

    seen = await sweep_tmux_terminals(
        manager,
        [FakeTmux("gobby", [promoted, spawning])],
        machine_id=LOCAL_MACHINE_ID,
        owners={},
        fallback_project_id=sample_project["id"],
    )

    assert set(seen) == {key_of(promoted), key_of(spawning)}
    assert live_external(manager) == {}
    refreshed = manager.get(live.id)
    assert refreshed is not None
    assert (refreshed.ownership, refreshed.state, refreshed.title) == (
        "gobby",
        "live",
        "agent title",
    )


@pytest.mark.asyncio
async def test_sweep_expires_vanished_panes_only_on_sockets_it_could_read(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    manager = TerminalManager(temp_db)
    gone = pane(DEFAULT_SOCKET, "%4")
    unreadable = pane(GOBBY_SOCKET, "%6", session_name="gobby-x")
    await sweep_tmux_terminals(
        manager,
        [FakeTmux("", [gone]), FakeTmux("gobby", [unreadable])],
        machine_id=LOCAL_MACHINE_ID,
        owners={},
        fallback_project_id=sample_project["id"],
    )
    before = live_external(manager)
    assert set(before) == {key_of(gone), key_of(unreadable)}

    seen = await sweep_tmux_terminals(
        manager,
        [FakeTmux("", []), FakeTmux("gobby", None)],
        machine_id=LOCAL_MACHINE_ID,
        owners={},
        fallback_project_id=sample_project["id"],
    )

    assert seen == {}
    exited = manager.get(before[key_of(gone)].id)
    assert exited is not None and exited.state == "exited"
    assert set(live_external(manager)) == {key_of(unreadable)}


@pytest.mark.asyncio
async def test_sweep_binds_the_session_recorded_in_a_pane(
    temp_db: HubDatabase, sample_project: dict[str, Any], session_manager: SessionManager
) -> None:
    manager = TerminalManager(temp_db)
    owned = pane(DEFAULT_SOCKET, "%7", session_name="7")
    session = session_manager.register(
        external_id="pane-owner",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
        terminal_context={"tmux_socket_path": DEFAULT_SOCKET, "tmux_pane": "%7"},
    )
    owners = pane_owners([session])
    assert owners == {
        (DEFAULT_SOCKET, "%7"): PaneOwner(session_id=session.id, project_id=sample_project["id"])
    }

    await sweep_tmux_terminals(
        manager,
        [FakeTmux("", [owned])],
        machine_id=LOCAL_MACHINE_ID,
        owners=owners,
        fallback_project_id=sample_project["id"],
    )

    row = live_external(manager)[key_of(owned)]
    assert row.session_id == session.id
    assert row.project_id == sample_project["id"]


def test_project_for_path_resolves_only_registered_projects(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    manager = TerminalManager(temp_db)
    registered = tmp_path / "registered"
    (registered / ".gobby").mkdir(parents=True)
    (registered / ".gobby" / "project.json").write_text(json.dumps({"id": sample_project["id"]}))
    unknown = tmp_path / "unknown"
    (unknown / ".gobby").mkdir(parents=True)
    (unknown / ".gobby" / "project.json").write_text(json.dumps({"id": str(uuid.uuid4())}))
    worktree = tmp_path / "worktree"
    (worktree / ".gobby").mkdir(parents=True)
    (worktree / ".gobby" / "project.json").write_text(
        json.dumps({"id": str(uuid.uuid4()), "parent_project_id": sample_project["id"]})
    )

    assert _project_for_path(manager, str(registered / "src")) == sample_project["id"]
    assert _project_for_path(manager, str(worktree)) == sample_project["id"]
    assert _project_for_path(manager, str(unknown)) is None
    assert _project_for_path(manager, None) is None
    assert _project_for_path(manager, str(tmp_path / "missing")) is None
